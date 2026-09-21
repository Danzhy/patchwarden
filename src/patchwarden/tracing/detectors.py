"""Failure detectors: pure functions over a finished run's rows, one flag per problem found.

They run at the end of every run (pipeline.run_fix) and write the `flags` table, which
`trace show` and `trace stats` read. Each looks at what the trace recorded, never at live
state, so they can be re-run on an old run too.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

from patchwarden.tracing.store import Run


@dataclass(frozen=True)
class Flag:
    detector: str
    finding_id: str | None
    detail: str


@dataclass
class RunRows:
    run: dict
    steps: list[dict]
    findings: list[dict]
    violations: list[dict]


def _out(step: dict) -> dict:
    out = json.loads(step["output_json"]) if step["output_json"] else None
    return out if isinstance(out, dict) else {}


def _in(step: dict) -> dict:
    out = json.loads(step["input_json"]) if step["input_json"] else None
    return out if isinstance(out, dict) else {}


def new_finding_introduced(r: RunRows) -> list[Flag]:
    return [
        Flag("new_finding_introduced", s["finding_id"], "; ".join(_out(s)["new_findings"]))
        for s in r.steps
        if s["node"] == "verify" and _out(s).get("new_findings")
    ]


def tests_broke(r: RunRows) -> list[Flag]:
    bad = ("failed", "timeout", "error")
    out = [
        Flag("tests_broke", s["finding_id"], _out(s).get("tests_detail", "")[:300])
        for s in r.steps
        if s["node"] == "verify" and _out(s).get("tests") in bad
    ]
    out += [
        Flag("tests_broke", None, f"after ruff's fixes: {_out(s).get('detail', '')[:300]}")
        for s in r.steps
        if s["node"] == "tests"
        and _in(s).get("stage") == "deterministic"
        and _out(s).get("status") in bad
    ]
    return out


def cheating_attempt(r: RunRows) -> list[Flag]:
    return [
        Flag("cheating_attempt", v["finding_id"], f"{v['kind']}: {v['detail']}")
        for v in r.violations
    ]


def round_limit_hit(r: RunRows) -> list[Flag]:
    return [
        Flag(
            "round_limit_hit",
            s["finding_id"],
            f"{_out(s)['fix_rounds']} of {_out(s)['max_fix_rounds']} rounds: {_out(s)['reason']}",
        )
        for s in r.steps
        if s["node"] == "finalize" and _out(s).get("round_limit_hit")
    ]


def edit_apply_failed(r: RunRows) -> list[Flag]:
    return [
        Flag("edit_apply_failed", s["finding_id"], s["error"].removeprefix("edit_apply_failed: "))
        for s in r.steps
        if s["node"] == "apply_edits" and (s["error"] or "").startswith("edit_apply_failed")
    ]


def triage_policy_disagreement(r: RunRows) -> list[Flag]:
    """The LLM wanted something less cautious than policy allows (clamp raised it)."""
    return [
        Flag(
            "triage_policy_disagreement",
            f["finding_id"],
            f"Triage said {f['triage_decision']}, policy made it {f['final_decision']}",
        )
        for f in r.findings
        if f["clamped"]
    ]


def cost_over_budget(r: RunRows) -> list[Flag]:
    errors = [s["error"] for s in r.steps if "budget_exceeded" in (s["error"] or "")]
    if r.run.get("outcome") != "budget_exceeded" and not errors:
        return []
    detail = errors[0] if errors else f"run cost ${r.run.get('cost_usd') or 0:.4f}"
    return [Flag("cost_over_budget", None, detail)]


def invalid_json_from_llm(r: RunRows) -> list[Flag]:
    return [
        Flag("invalid_json_from_llm", s["finding_id"], f"{s['node']}: {s['error']}")
        for s in r.steps
        if s["node"].startswith("llm:") and "invalid_json_from_llm" in (s["error"] or "")
    ]


_COVERED = ("edit_apply_failed", "invalid_json_from_llm", "budget_exceeded")


def step_error(r: RunRows) -> list[Flag]:
    """Any other step error, once: where a step failed because its child did (triage because
    its LLM call did), only the child is reported."""
    failed_parents = {s["parent_step_id"] for s in r.steps if s["error"]}
    return [
        Flag("step_error", s["finding_id"], f"{s['node']}: {s['error']}")
        for s in r.steps
        if s["error"]
        and s["step_id"] not in failed_parents
        and not any(k in s["error"] for k in _COVERED)
    ]


DETECTORS: list[Callable[[RunRows], list[Flag]]] = [
    new_finding_introduced,
    tests_broke,
    cheating_attempt,
    round_limit_hit,
    edit_apply_failed,
    triage_policy_disagreement,
    cost_over_budget,
    invalid_json_from_llm,
    step_error,
]


def detect(rows: RunRows) -> list[Flag]:
    return [flag for d in DETECTORS for flag in d(rows)]


def load_rows(run: Run) -> RunRows:
    store, rid = run.store, run.run_id
    [row] = store.rows("runs", rid)
    return RunRows(
        row, store.rows("steps", rid), store.rows("findings", rid), store.rows("violations", rid)
    )


def detect_and_store(run: Run) -> list[Flag]:
    flags = detect(load_rows(run))
    for f in flags:
        run.flag(f.finding_id, f.detector, f.detail)
    return flags
