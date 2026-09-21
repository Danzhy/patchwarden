"""`fix` end to end: scan -> workspace -> baseline tests -> ruff's safe fixes (+ tests) ->
per-finding graph (Triage, clamp, Fixer, apply, check_diff, verify, Verifier) -> report ->
failure detectors. Every stage is a trace step; every finding gets a row.

Findings are processed bottom-up within each file, so an applied edit mostly shifts lines of
findings that are already done; later findings are mapped through each kept edit and refreshed
from verify's re-scan.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from patchwarden.agents.graph import GraphDeps, build_finding_graph
from patchwarden.agents.prompts import prompt_version
from patchwarden.agents.reporter import render_fix_markdown
from patchwarden.analyzers import Analyzer
from patchwarden.config import Config
from patchwarden.deterministic import run_deterministic, undo_deterministic
from patchwarden.llm import BudgetExceeded, LLMClient
from patchwarden.models import (
    Finding,
    FindingOutcome,
    FindingStatus,
    PreClassKind,
    Region,
    TestStatus,
)
from patchwarden.scan import scan
from patchwarden.scope import git_sha
from patchwarden.tracing.detectors import Flag, detect_and_store
from patchwarden.tracing.store import Run, TraceStore
from patchwarden.verify import TestRunner, locate
from patchwarden.workspace import WorkspaceError, line_mapper, open_workspace

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
    apply_error: str | None = None  # --apply refused: the source changed since the scan
    test_command: str | None = None
    tests_skipped: str | None = None  # why the Fixer's changes weren't tested
    flags: list[Flag] = field(default_factory=list)

    def report(self, patch_path: str = "") -> str:
        info = {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "cost_usd": self.cost_usd,
            "patch": patch_path if self.patch else "",
            "test_command": self.test_command,
            "tests_skipped": self.tests_skipped,
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
        detect_and_store(run)
        raise
    totals = run.finish(res.outcome)
    res.cost_usd = totals["cost_usd"]
    res.flags = detect_and_store(run)
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
        tests = TestRunner(cfg, ws.root)
        if result.findings:
            _run_tests(run, tests, "baseline")
            if tests.skip_reason and cfg.test_command:
                warnings.append(f"{tests.skip_reason}; Fixer changes will only be suggested")
        with run.step("deterministic") as step:
            det = run_deterministic(ws, result, cfg, analyzers)
            step.output = det.model_dump(mode="json", exclude={"remaining"})
        if det.fixed_files:
            status = _run_tests(run, tests, "deterministic")
            if status not in (TestStatus.passed, TestStatus.skipped):
                det = undo_deterministic(ws, result, det, f"the tests {status} after ruff's fixes")
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
                tests=tests,
                current=_by_file(det.remaining),
                analyzers=analyzers,
            )
            graph = build_finding_graph(deps)
            for i, pending in enumerate(remaining):
                f = deps.findings[pending.fingerprint]  # regions may have moved; _shift_later
                if budget_hit:
                    o = _escalate(f, result, "cost budget exceeded before this finding")
                    run.finding(o)
                elif not ws.exists(f.file):
                    o = _escalate(f, result, f"{f.file} is not an editable UTF-8 .py file")
                    run.finding(o)
                else:
                    before = ws.read(f.file)
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
                            ws.write(f.file, before)  # an edit may be applied but unverified
                            o = _escalate(f, result, str(e))
                            run.finding(o)
                        step.output = {"status": o.status}
                    _shift_later(
                        deps.findings,
                        remaining[i + 1 :],
                        f.file,
                        before,
                        ws,
                        deps.current.get(f.file, []),
                        cfg.max_lines_changed,
                    )
                outcomes.append(o)

        patch = ws.diff()
        applied: list[str] = []
        apply_error = None
        if apply and patch:
            try:
                applied = ws.apply_to_source()
            except WorkspaceError as e:  # keep the patch and report; the spend isn't wasted
                apply_error = str(e)

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
        apply_error=apply_error,
        test_command=cfg.test_command,
        tests_skipped=tests.skip_reason,
    )


def _run_tests(run: Run, tests: TestRunner, stage: str) -> TestStatus:
    with run.step("tests", input={"stage": stage, "command": tests.cmd}) as step:
        status, detail = tests.baseline() if stage == "baseline" else tests.run()
        step.output = {"status": status, "detail": detail[-500:]}
    return status


def _by_file(findings: list[Finding]) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = {}
    for f in findings:
        out.setdefault(f.file, []).append(f)
    return out


def _shift_later(
    findings: dict[str, Finding],
    later: list[Finding],
    file: str,
    before: str,
    ws,
    current: list[Finding],
    radius: int,
):
    """Bottom-up order keeps later findings' lines valid unless an edit also touched lines
    above them (a new import at the top, say). Map their regions through the edit, then take
    the exact region from the re-scan (`current`) where the finding can be found there. The
    fingerprint stays the one from the scan: it's the finding's id in the trace."""
    after = ws.read(file)
    if after == before:
        return
    new_line = line_mapper(before, after)
    for g in later:
        if g.file == file:
            cur = findings[g.fingerprint]
            start, end = new_line(cur.region.start_line), new_line(cur.region.end_line)
            region = Region(
                **{**cur.region.model_dump(), "start_line": start, "end_line": max(start, end)}
            )
            moved = cur.model_copy(update={"region": region})
            i = locate(moved, current, radius)
            if i is not None:
                now = current[i]
                moved = moved.model_copy(
                    update={"region": now.region, "snippet": now.snippet, "message": now.message}
                )
            findings[g.fingerprint] = moved


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
