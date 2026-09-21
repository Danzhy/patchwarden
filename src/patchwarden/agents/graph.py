"""The per-finding LangGraph graph:

    triage -> clamp -> fixer -> apply --(edit error, 1 retry)--> fixer
       \\         \\        \\       \\
        +---------+--------+-------+--> finalize

The graph runs once per finding (pipeline.run_fix loops over findings bottom-up), so the state
stays small and serialisable. The workspace, LLM client, trace run and config are closure
dependencies, not state. M4 adds verify -> verifier between apply and finalize.

Every path that goes wrong ends in escalated or failed, never in an unreviewed change: a failed
LLM call, an edit that doesn't apply, or a diff that breaks policy leaves the workspace as it was.
"""

from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from patchwarden.agents.fixer import parse_proposal, propose_fix
from patchwarden.agents.triage import triage
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
    TriageOutput,
    Violation,
)
from patchwarden.policy import check_diff, clamp
from patchwarden.tracing.store import Run
from patchwarden.workspace import Workspace, unified_diff

MAX_EDIT_ATTEMPTS = 2  # the first try plus one retry with the error fed back


class FindingState(TypedDict, total=False):
    fp: str
    parent: int | None
    triage: dict | None
    clamp: dict | None
    reply: str | None  # the Fixer's latest raw reply
    attempts: int  # edit attempts so far
    feedback: list[list[str]]  # [reply, error] pairs for the Fixer's retry
    edit_error: str | None
    rationale: str
    diff: str
    violations: list[dict]
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


def build_finding_graph(deps: GraphDeps):
    ws, llm, run, cfg = deps.ws, deps.llm, deps.run, deps.cfg

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
        feedback = [(r, e) for r, e in state.get("feedback", [])]
        with run.step(
            "fixer",
            finding_id=f.fingerprint,
            parent=state.get("parent"),
            input={"attempt": state.get("attempts", 0) + 1, "feedback": [e for _, e in feedback]},
        ) as step:
            try:
                reply = propose_fix(llm, ws, f, feedback, step.step_id)
            except BudgetExceeded:
                raise
            except LLMError as e:
                step.error = str(e)
                return {"reply": None, "error": f"fixer failed ({e})"}
            step.output = {"reply": reply}
        return {"reply": reply}

    def apply_node(state: FindingState) -> FindingState:
        f = deps.findings[state["fp"]]
        attempts = state.get("attempts", 0) + 1
        reply = state["reply"] or ""
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
                    "attempts": attempts,
                    "edit_error": str(e),
                    "feedback": [*state.get("feedback", []), [reply, str(e)]],
                }
            step.output = {"files": list(changes), "rationale": proposal.rationale}
        with run.step("check_diff", finding_id=f.fingerprint, parent=state.get("parent")) as step:
            violations = check_diff(
                changes,
                cfg,
                target_file=f.file,
                rule_ids={f.rule_id},
                max_lines=cfg.max_lines_changed,
            )
            step.output = {"violations": [v.model_dump(mode="json") for v in violations]}
        diff = "".join(unified_diff(rel, b, a) for rel, (b, a) in changes.items())
        if violations or state["clamp"]["decision"] == Decision.suggest:
            # Rejected, or only a suggestion: the patch must not contain it.
            for rel, (before, _) in changes.items():
                ws.write(rel, before)
        return {
            "attempts": attempts,
            "edit_error": None,
            "rationale": proposal.rationale,
            "diff": diff,
            "violations": [v.model_dump(mode="json") for v in violations],
        }

    def finalize_node(state: FindingState) -> FindingState:
        o = outcome_of(state, deps)
        for v in o.violations:
            run.violation(o.finding.fingerprint, v)
        run.finding(o)
        return {"outcome": o.model_dump(mode="json")}

    def after_triage(state: FindingState) -> str:
        return "clamp" if state.get("triage") else "finalize"

    def after_clamp(state: FindingState) -> str:
        fixable = (Decision.auto_fix, Decision.suggest)
        return "fixer" if state["clamp"]["decision"] in fixable else "finalize"

    def after_fixer(state: FindingState) -> str:
        return "apply" if state.get("reply") else "finalize"

    def after_apply(state: FindingState) -> str:
        retry = state.get("edit_error") and state["attempts"] < MAX_EDIT_ATTEMPTS
        return "fixer" if retry else "finalize"

    g = StateGraph(FindingState)
    g.add_node("triage", triage_node)
    g.add_node("clamp", clamp_node)
    g.add_node("fixer", fixer_node)
    g.add_node("apply", apply_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", after_triage, ["clamp", "finalize"])
    g.add_conditional_edges("clamp", after_clamp, ["fixer", "finalize"])
    g.add_conditional_edges("fixer", after_fixer, ["apply", "finalize"])
    g.add_conditional_edges("apply", after_apply, ["fixer", "finalize"])
    g.add_edge("finalize", END)
    return g.compile()


def outcome_of(state: FindingState, deps: GraphDeps) -> FindingOutcome:
    f, pc = deps.findings[state["fp"]], deps.preclass[state["fp"]]
    tri = TriageOutput.model_validate(state["triage"]) if state.get("triage") else None
    cl = ClampResult.model_validate(state["clamp"]) if state.get("clamp") else None
    violations = [Violation.model_validate(v) for v in state.get("violations", [])]
    base = {
        "finding": f,
        "preclass": pc,
        "triage": tri,
        "clamp": cl,
        "fix_rounds": state.get("attempts", 0),
        "rationale": state.get("rationale", ""),
        "diff": state.get("diff", ""),
        "violations": violations,
        "error": state.get("error") or state.get("edit_error"),
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
        why = f"no applicable edit after {state['attempts']} attempts: {state['edit_error']}"
        return FindingOutcome(**base, status=FindingStatus.failed, reason=why)
    if violations:
        kinds = ", ".join(sorted({v.kind for v in violations}))
        why = f"fix rejected by policy ({kinds})"
        return FindingOutcome(**base, status=FindingStatus.escalated, reason=why)
    if cl.decision == Decision.auto_fix:
        return FindingOutcome(
            **base, status=FindingStatus.fixed, fixed_by="fixer", reason=tri.reason
        )
    why = cl.reason if cl.clamped else tri.reason
    return FindingOutcome(**base, status=FindingStatus.suggested, reason=why)
