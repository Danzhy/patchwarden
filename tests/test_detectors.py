"""The failure detectors as pure functions over hand-made trace rows."""

import json

from patchwarden.tracing.detectors import DETECTORS, RunRows, detect

SPEC = {
    "new_finding_introduced",
    "tests_broke",
    "cheating_attempt",
    "round_limit_hit",
    "edit_apply_failed",
    "triage_policy_disagreement",
    "cost_over_budget",
    "invalid_json_from_llm",
    "step_error",
}


def step(step_id, node, *, parent=None, fid="fp1", out=None, inp=None, error=None):
    return {
        "step_id": step_id,
        "parent_step_id": parent,
        "node": node,
        "finding_id": fid,
        "input_json": json.dumps(inp) if inp is not None else None,
        "output_json": json.dumps(out) if out is not None else None,
        "error": error,
    }


def rows(steps=(), findings=(), violations=(), outcome="escalations"):
    return RunRows(
        {"outcome": outcome, "cost_usd": 0.01}, list(steps), list(findings), list(violations)
    )


def names(flags):
    return [f.detector for f in flags]


def test_all_nine_detectors_exist():
    assert {d.__name__ for d in DETECTORS} == SPEC


def test_clean_run_has_no_flags():
    ok = rows(
        steps=[
            step(1, "finding"),
            step(
                2,
                "verify",
                parent=1,
                out={"syntax_ok": True, "new_findings": [], "tests": "passed"},
            ),
            step(3, "finalize", parent=1, out={"status": "fixed", "round_limit_hit": False}),
        ],
        findings=[{"finding_id": "fp1", "clamped": 0}],
        outcome="ok",
    )
    assert detect(ok) == []


def test_verify_failures():
    r = rows(
        steps=[
            step(1, "verify", out={"new_findings": ["ruff:F541 at m.py:2: x"], "tests": "skipped"}),
            step(
                2, "verify", out={"new_findings": [], "tests": "failed", "tests_detail": "exit 1"}
            ),
            step(3, "tests", fid=None, inp={"stage": "deterministic"}, out={"status": "failed"}),
            step(4, "tests", fid=None, inp={"stage": "baseline"}, out={"status": "failed"}),
        ]
    )
    flags = detect(r)
    assert names(flags) == ["new_finding_introduced", "tests_broke", "tests_broke"]
    assert flags[0].detail == "ruff:F541 at m.py:2: x"
    assert flags[2].finding_id is None and "ruff's fixes" in flags[2].detail  # not the baseline


def test_violation_is_a_cheating_attempt():
    [f] = detect(
        rows(
            violations=[{"finding_id": "fp1", "kind": "suppression_added", "detail": "m.py: noqa"}]
        )
    )
    assert (f.detector, f.finding_id, f.detail) == (
        "cheating_attempt",
        "fp1",
        "suppression_added: m.py: noqa",
    )


def test_round_limit_and_edit_failures():
    r = rows(
        steps=[
            step(
                1, "apply_edits", error="edit_apply_failed: no_match: m.py (search text not found)"
            ),
            step(
                2, "apply_edits", error="edit_apply_failed: no_match: m.py (search text not found)"
            ),
            step(
                3,
                "finalize",
                out={
                    "status": "failed",
                    "fix_rounds": 2,
                    "max_fix_rounds": 2,
                    "round_limit_hit": True,
                    "reason": "no edit",
                },
            ),
        ]
    )
    assert names(detect(r)) == ["round_limit_hit", "edit_apply_failed", "edit_apply_failed"]
    assert detect(r)[0].detail == "2 of 2 rounds: no edit"


def test_triage_policy_disagreement():
    r = rows(
        findings=[
            {
                "finding_id": "a",
                "clamped": 1,
                "triage_decision": "auto_fix",
                "final_decision": "escalate",
            },
            {
                "finding_id": "b",
                "clamped": 0,
                "triage_decision": "suggest",
                "final_decision": "suggest",
            },
            {"finding_id": "c", "clamped": None, "triage_decision": None, "final_decision": None},
        ]
    )
    [f] = detect(r)
    assert f.finding_id == "a" and f.detail == "Triage said auto_fix, policy made it escalate"


def test_budget_flagged_once_and_not_as_step_errors():
    r = rows(
        steps=[
            step(1, "finding", error="budget_exceeded: spent $0.30 of $0.25"),
            step(2, "triage", parent=1, error="BudgetExceeded: budget_exceeded: spent"),
            step(3, "llm:triage", parent=2, error="BudgetExceeded: budget_exceeded: spent"),
        ],
        outcome="budget_exceeded",
    )
    assert names(detect(r)) == ["cost_over_budget"]
    assert names(detect(rows(outcome="budget_exceeded"))) == ["cost_over_budget"]


def test_llm_errors_reported_at_the_deepest_step():
    r = rows(
        steps=[
            step(1, "triage", error="invalid_json_from_llm: decision: bad"),
            step(2, "llm:triage", parent=1, error="LLMError: invalid_json_from_llm: decision: bad"),
            step(3, "fixer", error="api_error: HTTP 400"),
            step(4, "llm:fixer", parent=3, error="LLMError: api_error: HTTP 400"),
            step(5, "scan", fid=None, error="AnalyzerError: ruff exited 2"),
        ]
    )
    flags = detect(r)
    assert names(flags) == ["invalid_json_from_llm", "step_error", "step_error"]
    assert [f.detail for f in flags[1:]] == [
        "llm:fixer: LLMError: api_error: HTTP 400",
        "scan: AnalyzerError: ruff exited 2",
    ]
