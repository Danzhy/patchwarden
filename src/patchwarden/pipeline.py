"""`fix` end to end: scan -> workspace -> ruff's safe fixes -> per-finding graph (Triage, clamp,
Fixer, apply, check_diff) -> report. Every stage is a trace step; every finding gets a row.

Findings are processed bottom-up within each file, so an applied edit only shifts lines of
findings that are already done.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from patchwarden.agents.graph import GraphDeps, build_finding_graph
from patchwarden.agents.prompts import prompt_version
from patchwarden.agents.reporter import render_fix_markdown
from patchwarden.analyzers import Analyzer
from patchwarden.config import Config
from patchwarden.deterministic import run_deterministic
from patchwarden.llm import BudgetExceeded, LLMClient
from patchwarden.models import Finding, FindingOutcome, FindingStatus, PreClassKind
from patchwarden.scan import scan
from patchwarden.scope import git_sha
from patchwarden.tracing.store import Run, TraceStore
from patchwarden.workspace import open_workspace

LLMFactory = Callable[[Config, Run], LLMClient]
ESCALATE_KINDS = (PreClassKind.always_escalate, PreClassKind.protected_path)


@dataclass
class FixResult:
    run_id: str
    outcome: str
    outcomes: list[FindingOutcome]
    patch: str
    cost_usd: float
    ruff_candidates: int = 0
    reverted: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def report(self, patch_path: str = "") -> str:
        info = {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "cost_usd": self.cost_usd,
            "patch": patch_path if self.patch else "",
        }
        return render_fix_markdown(self.outcomes, info)

    @property
    def needs_human(self) -> bool:
        return any(o.needs_human for o in self.outcomes)


def run_fix(
    repo: Path,
    cfg: Config,
    *,
    store: TraceStore,
    llm_factory: LLMFactory,
    base: str | None = None,
    no_llm: bool = False,
    apply: bool = False,
    trigger: str = "cli",
    analyzers: list[Analyzer] | None = None,
) -> FixResult:
    repo = repo.resolve()
    run = store.start_run(
        repo=str(repo),
        trigger=trigger,
        config_hash=cfg.config_hash(),
        prompt_version=prompt_version(),
        models=cfg.models,
        base=base,
        git_sha=git_sha(repo),
    )
    try:
        res = _run(run, repo, cfg, llm_factory, base, no_llm, apply, analyzers)
    except BaseException:
        run.finish("error")
        raise
    totals = run.finish(res.outcome)
    res.cost_usd = totals["cost_usd"]
    return res


def _run(run, repo, cfg, llm_factory, base, no_llm, apply, analyzers) -> FixResult:
    with run.step("scan", input={"base": base}) as step:
        result, warnings = scan(repo, base=base, cfg=cfg, analyzers=analyzers)
        step.output = {
            "files": len(result.files_scanned),
            "findings": len(result.findings),
            "analyzers": result.analyzers_run,
            "skipped": result.analyzers_skipped,
        }
    by_fp = {f.fingerprint: f for f in result.findings}
    outcomes: list[FindingOutcome] = []
    budget_hit = False

    with open_workspace(repo) as ws:
        with run.step("deterministic") as step:
            det = run_deterministic(ws, result, cfg, analyzers)
            step.output = det.model_dump(mode="json", exclude={"remaining"})
        for fp in det.resolved:
            o = FindingOutcome(
                finding=by_fp[fp],
                preclass=result.preclass[fp],
                status=FindingStatus.fixed,
                fixed_by="ruff",
                reason="ruff safe fix",
            )
            outcomes.append(o)
            run.finding(o)

        remaining = sorted(det.remaining, key=lambda f: (f.file, -f.region.start_line))
        if no_llm:
            for f in remaining:
                o = _without_llm(f, result.preclass[f.fingerprint])
                outcomes.append(o)
                run.finding(o)
        elif remaining:
            llm = llm_factory(cfg, run)
            deps = GraphDeps(
                ws=ws,
                llm=llm,
                run=run,
                cfg=cfg,
                findings={f.fingerprint: f for f in remaining},
                preclass=result.preclass,
            )
            graph = build_finding_graph(deps)
            for f in remaining:
                if budget_hit:
                    o = _escalate(f, result, "cost budget exceeded before this finding")
                    run.finding(o)
                else:
                    with run.step(
                        "finding",
                        finding_id=f.fingerprint,
                        input={"rule_id": f.rule_id, "location": f.location},
                    ) as step:
                        try:
                            state = graph.invoke({"fp": f.fingerprint, "parent": step.step_id})
                            o = FindingOutcome.model_validate(state["outcome"])
                        except BudgetExceeded as e:
                            budget_hit = True
                            step.error = str(e)
                            o = _escalate(f, result, str(e))
                            run.finding(o)
                        step.output = {"status": o.status}
                outcomes.append(o)

        patch = ws.diff()
        applied = ws.apply_to_source() if apply and patch else []

    outcomes.sort(key=lambda o: (o.finding.file, o.finding.region.start_line, o.finding.rule_id))
    if budget_hit:
        outcome = "budget_exceeded"
    elif any(o.needs_human for o in outcomes):
        outcome = "escalations"
    else:
        outcome = "ok" if outcomes else "clean"
    return FixResult(
        run_id=run.run_id,
        outcome=outcome,
        outcomes=outcomes,
        patch=patch,
        cost_usd=0.0,
        ruff_candidates=sum(
            1
            for f in result.findings
            if f.tool == "ruff"
            and result.preclass[f.fingerprint].kind == PreClassKind.auto_fix_allowed
        ),
        reverted=det.reverted,
        warnings=warnings,
        applied=applied,
    )


def _escalate(f: Finding, result, reason: str) -> FindingOutcome:
    return FindingOutcome(
        finding=f,
        preclass=result.preclass[f.fingerprint],
        status=FindingStatus.escalated,
        reason=reason,
    )


def _without_llm(f: Finding, pc) -> FindingOutcome:
    """--no-llm: policy alone. Escalations still escalate; the rest waits for triage."""
    status = FindingStatus.escalated if pc.kind in ESCALATE_KINDS else FindingStatus.not_triaged
    return FindingOutcome(finding=f, preclass=pc, status=status, reason=pc.reason)
