"""The per-finding LangGraph graph:

    triage -> clamp -> fixer -> apply -> verify -> verifier -> finalize
                         ^        |         |          |
                         +--------+---------+----------+   failed: retry with feedback,
                                                            up to max_fix_rounds Fixer calls

- apply: parse the SEARCH/REPLACE blocks, apply them, run check_diff. An edit that doesn't apply
  is retried; a policy violation (suppression, test edit, other file, ...) is a cheating attempt
  and escalates at once, with no retry.
- verify: code checks (verify.verify_fix): parses, re-scan, tests.
- verifier: the LLM review, only once the code checks pass.

The graph runs once per finding (pipeline.run_fix loops over findings bottom-up), so the state
stays small and serialisable. The workspace, LLM client, trace run and config are closure
dependencies, not state.

Every path that goes wrong ends in escalated or failed, never in an unreviewed change: a failed
LLM call, an edit that doesn't apply, a diff that breaks policy or a fix that fails a check
leaves the file as it was. Only an auto_fix whose tests ran and passed, and that the Verifier
passed without rating the risk high, stays in the workspace (and so in the patch).
"""

from dataclasses import dataclass, field
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from patchwarden.agents.fixer import parse_proposal, propose_fix
from patchwarden.agents.triage import triage
from patchwarden.agents.verifier import review
from patchwarden.analyzers import Analyzer
from patchwarden.config import Config
from patchwarden.edits import EditApplyError, EditParseError, apply_edits
from patchwarden.llm import BudgetExceeded, LLMClient, LLMError
from patchwarden.models import (
    ClampResult,
    Decision,
    Finding,
    FindingOutcome,
    FindingStatus,
    PreClass,
    TestStatus,
    TriageOutput,
    VerifierOutput,
    VerifyResult,
    Violation,
    ViolationKind,
)
from patchwarden.policy import check_diff, clamp
from patchwarden.tracing.store import Run
from patchwarden.verify import TestRunner, verify_fix
from patchwarden.workspace import Workspace, unified_diff

VERIFIER_CONTEXT_LINES = 10  # the Verifier judges behaviour, so it gets more context


class FindingState(TypedDict, total=False):
    fp: str
    parent: int | None
    triage: dict | None
    clamp: dict | None
    reply: str | None  # the Fixer's latest raw reply
    attempts: int  # Fixer calls so far
    feedback: list[list[str]]  # [reply, what was wrong] pairs for the Fixer's retry
    edit_error: str | None  # the latest edit didn't apply
    failure: str | None  # the latest fix failed verify or the Verifier
    before: str  # the target file before the latest edit
    rationale: str
    diff: str
    violations: list[dict]
    verify: dict | None
    verifier: dict | None
    kept: bool  # the fix stays in the workspace (goes into the patch)
    error: str | None  # an LLM failure; the finding is escalated
    outcome: dict


@dataclass
class GraphDeps:
    ws: Workspace
    llm: LLMClient
    run: Run
    cfg: Config
    findings: dict[str, Finding]
    preclass: dict[str, PreClass]
    tests: TestRunner
    # file -> its findings as the file is now. verify compares against it; a kept fix
    # replaces it with the re-scan.
    current: dict[str, list[Finding]] = field(default_factory=dict)
    analyzers: list[Analyzer] | None = None


def build_finding_graph(deps: GraphDeps):
    ws, llm, run, cfg = deps.ws, deps.llm, deps.run, deps.cfg
    max_rounds = max(1, cfg.max_fix_rounds)
    rescans: dict[str, list[Finding]] = {}  # fp -> the target file's findings after the fix

    def triage_node(state: FindingState) -> FindingState:
        f, pc = deps.findings[state["fp"]], deps.preclass[state["fp"]]
        with run.step(
            "triage",
            finding_id=f.fingerprint,
            parent=state.get("parent"),
            input={"rule_id": f.rule_id, "location": f.location, "pre_class": pc.kind},
        ) as step:
            try:
                out = triage(llm, ws, f, pc, step.step_id)
            except BudgetExceeded:
                raise
            except LLMError as e:
                step.error = str(e)
                return {"triage": None, "error": f"triage failed ({e})"}
            step.output = out.model_dump(mode="json")
        return {"triage": out.model_dump(mode="json")}

    def clamp_node(state: FindingState) -> FindingState:
        pc = deps.preclass[state["fp"]]
        decision = Decision(state["triage"]["decision"])
        res = clamp(decision, pc)
        with run.step(
            "clamp",
            finding_id=state["fp"],
            parent=state.get("parent"),
            input={"llm_decision": decision, "pre_class": pc.kind},
        ) as step:
            step.output = res.model_dump(mode="json")
        return {"clamp": res.model_dump(mode="json")}

    def fixer_node(state: FindingState) -> FindingState:
        f = deps.findings[state["fp"]]
        attempt = state.get("attempts", 0) + 1
        feedback = [(r, e) for r, e in state.get("feedback", [])]
        with run.step(
            "fixer",
            finding_id=f.fingerprint,
            parent=state.get("parent"),
            input={"attempt": attempt, "feedback": [e for _, e in feedback]},
        ) as step:
            try:
                reply = propose_fix(llm, ws, f, feedback, step.step_id)
            except BudgetExceeded:
                raise
            except LLMError as e:
                step.error = str(e)
                return {"attempts": attempt, "reply": None, "error": f"fixer failed ({e})"}
            step.output = {"reply": reply}
        return {
            "attempts": attempt,
            "reply": reply,
            "edit_error": None,
            "failure": None,
            "verify": None,
            "verifier": None,
        }

    def apply_node(state: FindingState) -> FindingState:
        f = deps.findings[state["fp"]]
        reply = state["reply"] or ""
        before = ws.read(f.file)
        with run.step("apply_edits", finding_id=f.fingerprint, parent=state.get("parent")) as step:
            try:
                proposal = parse_proposal(reply)
                changes = apply_edits(ws, proposal.blocks)
                if not changes:
                    raise EditParseError("the edit blocks don't change anything")
            except (EditParseError, EditApplyError) as e:
                kind = getattr(e, "kind", "parse_error")
                step.error = f"edit_apply_failed: {kind}: {e}"
                return {
                    "edit_error": str(e),
                    "feedback": [*state.get("feedback", []), [reply, str(e)]],
                }
            step.output = {"files": list(changes), "rationale": proposal.rationale}
        with run.step("check_diff", finding_id=f.fingerprint, parent=state.get("parent")) as step:
            found = check_diff(
                changes,
                cfg,
                target_file=f.file,
                rule_ids={f.rule_id},
                max_lines=cfg.max_lines_changed,
            )
            # A syntax error is a broken fix, not a cheating attempt: verify reports it and the
            # Fixer gets another round.
            violations = [v for v in found if v.kind != ViolationKind.syntax_error]
            step.output = {"violations": [v.model_dump(mode="json") for v in violations]}
        diff = "".join(unified_diff(rel, b, a) for rel, (b, a) in changes.items())
        if violations:
            for rel, (old, _) in changes.items():
                ws.write(rel, old)
        return {
            "before": before,
            "rationale": proposal.rationale,
            "diff": diff,
            "violations": [v.model_dump(mode="json") for v in violations],
        }

    def verify_node(state: FindingState) -> FindingState:
        f = deps.findings[state["fp"]]
        with run.step(
            "verify",
            finding_id=f.fingerprint,
            parent=state.get("parent"),
            input={"attempt": state["attempts"]},
        ) as step:
            res, after = verify_fix(
                ws,
                f,
                state["before"],
                deps.current.get(f.file, []),
                cfg,
                deps.tests,
                deps.analyzers,
            )
            step.output = res.model_dump(mode="json")
        if res.passed:
            rescans[state["fp"]] = after or []
            return {"verify": res.model_dump(mode="json")}
        ws.write(f.file, state["before"])
        why = "; ".join(res.failures())
        return {
            "verify": res.model_dump(mode="json"),
            "failure": why,
            "feedback": [*state.get("feedback", []), [state["reply"] or "", why]],
        }

    def verifier_node(state: FindingState) -> FindingState:
        f = deps.findings[state["fp"]]
        res = VerifyResult.model_validate(state["verify"])
        diff = unified_diff(f.file, state["before"], ws.read(f.file), VERIFIER_CONTEXT_LINES)
        with run.step("verifier", finding_id=f.fingerprint, parent=state.get("parent")) as step:
            try:
                out = review(llm, f, diff, res, cfg.test_command, step.step_id)
            except BudgetExceeded:
                raise
            except LLMError as e:
                step.error = str(e)
                ws.write(f.file, state["before"])
                rescans.pop(state["fp"], None)
                return {"error": f"verifier failed ({e})"}
            step.output = out.model_dump(mode="json")
        after = rescans.pop(state["fp"], [])
        if out.verdict == "fail":
            ws.write(f.file, state["before"])
            why = f"an independent review rejected it: {out.reason}"
            return {
                "verifier": out.model_dump(mode="json"),
                "failure": why,
                "feedback": [*state.get("feedback", []), [state["reply"] or "", why]],
            }
        kept = (
            state["clamp"]["decision"] == Decision.auto_fix
            and res.tests == TestStatus.passed
            and out.behaviour_change_risk != "high"
        )
        if kept:
            deps.current[f.file] = after
        else:  # a suggestion: the report shows the diff, the patch doesn't have it
            ws.write(f.file, state["before"])
        return {"verifier": out.model_dump(mode="json"), "kept": kept}

    def finalize_node(state: FindingState) -> FindingState:
        o = outcome_of(state, deps)
        limit_hit = bool(state.get("edit_error") or state.get("failure")) and (
            state.get("attempts", 0) >= max_rounds
        )
        with run.step("finalize", finding_id=state["fp"], parent=state.get("parent")) as step:
            step.output = {
                "status": o.status,
                "fix_rounds": o.fix_rounds,
                "max_fix_rounds": max_rounds,
                "round_limit_hit": limit_hit,
                "reason": o.reason,
            }
        for v in o.violations:
            run.violation(o.finding.fingerprint, v)
        run.finding(o)
        return {"outcome": o.model_dump(mode="json")}

    def retry_or_stop(state: FindingState) -> str:
        return "fixer" if state.get("attempts", 0) < max_rounds else "finalize"

    def after_triage(state: FindingState) -> str:
        return "clamp" if state.get("triage") else "finalize"

    def after_clamp(state: FindingState) -> str:
        fixable = (Decision.auto_fix, Decision.suggest)
        return "fixer" if state["clamp"]["decision"] in fixable else "finalize"

    def after_fixer(state: FindingState) -> str:
        return "apply" if state.get("reply") else "finalize"

    def after_apply(state: FindingState) -> str:
        if state.get("edit_error"):
            return retry_or_stop(state)
        return "finalize" if state.get("violations") else "verify"

    def after_verify(state: FindingState) -> str:
        return retry_or_stop(state) if state.get("failure") else "verifier"

    def after_verifier(state: FindingState) -> str:
        return retry_or_stop(state) if state.get("failure") else "finalize"

    g = StateGraph(FindingState)
    for name, node in [
        ("triage", triage_node),
        ("clamp", clamp_node),
        ("fixer", fixer_node),
        ("apply", apply_node),
        ("verify", verify_node),
        ("verifier", verifier_node),
        ("finalize", finalize_node),
    ]:
        g.add_node(name, node)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", after_triage, ["clamp", "finalize"])
    g.add_conditional_edges("clamp", after_clamp, ["fixer", "finalize"])
    g.add_conditional_edges("fixer", after_fixer, ["apply", "finalize"])
    g.add_conditional_edges("apply", after_apply, ["fixer", "verify", "finalize"])
    g.add_conditional_edges("verify", after_verify, ["fixer", "verifier", "finalize"])
    g.add_conditional_edges("verifier", after_verifier, ["fixer", "finalize"])
    g.add_edge("finalize", END)
    return g.compile()


def outcome_of(state: FindingState, deps: GraphDeps) -> FindingOutcome:
    f, pc = deps.findings[state["fp"]], deps.preclass[state["fp"]]
    tri = TriageOutput.model_validate(state["triage"]) if state.get("triage") else None
    cl = ClampResult.model_validate(state["clamp"]) if state.get("clamp") else None
    ver = VerifyResult.model_validate(state["verify"]) if state.get("verify") else None
    rev = VerifierOutput.model_validate(state["verifier"]) if state.get("verifier") else None
    violations = [Violation.model_validate(v) for v in state.get("violations", [])]
    rounds = state.get("attempts", 0)
    base = {
        "finding": f,
        "preclass": pc,
        "triage": tri,
        "clamp": cl,
        "fix_rounds": rounds,
        "rationale": state.get("rationale", ""),
        "diff": state.get("diff", ""),
        "violations": violations,
        "verify": ver,
        "verifier": rev,
        "error": state.get("error") or state.get("edit_error") or state.get("failure"),
    }
    if tri is None or cl is None:
        return FindingOutcome(**base, status=FindingStatus.escalated, reason=state["error"])
    if cl.decision == Decision.escalate:
        why = cl.reason if cl.clamped else tri.reason
        return FindingOutcome(**base, status=FindingStatus.escalated, reason=why)
    if cl.decision == Decision.false_positive:
        return FindingOutcome(**base, status=FindingStatus.false_positive, reason=tri.reason)
    if state.get("error"):
        return FindingOutcome(**base, status=FindingStatus.escalated, reason=state["error"])
    if state.get("edit_error"):
        why = f"no applicable edit after {rounds} attempt(s): {state['edit_error']}"
        return FindingOutcome(**base, status=FindingStatus.failed, reason=why)
    if violations:
        kinds = ", ".join(sorted({v.kind for v in violations}))
        why = f"fix rejected by policy ({kinds})"
        return FindingOutcome(**base, status=FindingStatus.escalated, reason=why)
    if state.get("failure"):
        why = f"no fix passed verification in {rounds} round(s); last: {state['failure']}"
        return FindingOutcome(**base, status=FindingStatus.escalated, reason=why)
    if state.get("kept"):
        return FindingOutcome(
            **base, status=FindingStatus.fixed, fixed_by="fixer", reason=tri.reason
        )
    if cl.decision == Decision.suggest:
        why = cl.reason if cl.clamped else tri.reason
    elif ver is not None and ver.tests != TestStatus.passed:
        why = f"untested, so not applied automatically: {ver.tests_detail}"
    else:
        risk = rev.reason if rev else ""
        why = f"the Verifier rates the behaviour-change risk high: {risk}"
    return FindingOutcome(**base, status=FindingStatus.suggested, reason=why)
