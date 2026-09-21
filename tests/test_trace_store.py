import json
import re

import pytest

from patchwarden.models import (
    Finding,
    FindingOutcome,
    FindingStatus,
    PreClass,
    Region,
    Violation,
)
from patchwarden.tracing.store import TraceStore, redact


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "t")
    yield s
    s.close()


def start(store):
    return store.start_run(
        repo="/r", trigger="cli", config_hash="abc", prompt_version="m3.1+x", models={"a": "b"}
    )


def outcome():
    f = Finding(
        tool="ruff",
        rule_id="ruff:F841",
        message="m",
        file="a.py",
        region=Region(start_line=3, end_line=3),
        snippet="x = 1\n",
        fingerprint="fp1",
    )
    return FindingOutcome(
        finding=f,
        preclass=PreClass(kind="auto_fix_allowed", reason="allowlisted"),
        status=FindingStatus.escalated,
        reason="why",
    )


def test_schema_tables(store):
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"runs", "steps", "findings", "violations", "flags"}


def test_run_id_format(store):
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", start(store).run_id)


def test_steps_findings_violations_and_totals(store):
    run = start(store)
    with run.step("finding", finding_id="fp1") as parent:
        child = run.step("llm:triage", finding_id="fp1", parent=parent.step_id, input={"q": 1})
        with child as s:
            s.tokens_in, s.tokens_out, s.cost, s.model = 10, 5, 0.25, "m"
            s.output = {"a": 1}
    run.finding(outcome())
    run.violation("fp1", Violation(kind="suppression_added", file="a.py", detail="noqa"))
    totals = run.finish("escalations")

    steps = store.rows("steps", run.run_id)
    # The inner step is written first (on exit) but keeps the id allocated on entry.
    assert [(s["step_id"], s["node"], s["parent_step_id"]) for s in steps] == [
        (1, "finding", None),
        (2, "llm:triage", 1),
    ]
    assert json.loads(steps[1]["input_json"]) == {"q": 1}
    assert steps[1]["latency_ms"] >= 0
    [f] = store.rows("findings", run.run_id)
    assert (f["rule_id"], f["line"], f["pre_class"], f["status"]) == (
        "ruff:F841",
        3,
        "auto_fix_allowed",
        "escalated",
    )
    [v] = store.rows("violations", run.run_id)
    assert v["detail"] == "a.py: noqa"
    [r] = store.rows("runs", run.run_id)
    assert (r["outcome"], r["tokens_in"], r["cost_usd"]) == ("escalations", 10, 0.25)
    assert totals["cost_usd"] == 0.25 and json.loads(r["models_json"]) == {"a": "b"}


def test_step_records_exception(store):
    run = start(store)
    with pytest.raises(ValueError), run.step("scan"):
        raise ValueError("boom")
    [s] = store.rows("steps", run.run_id)
    assert s["error"] == "ValueError: boom"


def test_jsonl_mirrors_db(store):
    run = start(store)
    with run.step("scan") as s:
        s.output = {"n": 1}
    run.finding(outcome())
    run.finish("ok")
    lines = [json.loads(ln) for ln in run.jsonl.read_text().splitlines()]
    assert [ln["table"] for ln in lines] == ["runs", "steps", "findings", "runs_update"]
    db_step = store.rows("steps", run.run_id)[0]
    assert {k: v for k, v in lines[1].items() if k != "table"} == db_step


def test_redact(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "custom-key-123456")
    text = "k=custom-key-123456 sk-or-v1-abcdefghijkl Authorization: Bearer xyz.abc"
    assert redact(text) == "k=[REDACTED] [REDACTED] Authorization: [REDACTED]"
