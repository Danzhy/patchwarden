"""The per-finding graph (Triage -> clamp -> Fixer -> apply -> check_diff), driven through
run_fix on tiny repos with a scripted LLM. Real ruff and bandit, no network."""

import pytest
from fake_llm import FakeTransport, fake_client, fixer_reply, triage_json

from patchwarden.config import Config
from patchwarden.llm import LLMError, RawReply
from patchwarden.models import FindingStatus
from patchwarden.pipeline import run_fix
from patchwarden.tracing.store import TraceStore

F841_SRC = "def f():\n    unused = 0\n    return 1\n"
B006_SRC = "def add(x, xs=[]):\n    xs.append(x)\n    return xs\n"


def make_repo(tmp_path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    return repo


def fix(tmp_path, files, script, cfg=None, **kw):
    repo = make_repo(tmp_path, files)
    transport = FakeTransport(script)
    store = TraceStore(tmp_path / "trace")
    res = run_fix(
        repo,
        cfg or Config(),
        store=store,
        llm_factory=lambda c, run: fake_client(transport, c, run),
        **kw,
    )
    return res, transport, store, repo


def only(res):
    assert len(res.outcomes) == 1, res.outcomes
    return res.outcomes[0]


def test_auto_fix_goes_into_the_patch(tmp_path):
    res, t, store, repo = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): fixer_reply("app/mod.py", "    unused = 0\n", ""),
        },
    )
    o = only(res)
    assert o.status == FindingStatus.fixed and o.fixed_by == "fixer"
    assert o.rationale == "scripted fix" and o.fix_rounds == 1
    assert "-    unused = 0" in res.patch
    assert (repo / "app/mod.py").read_text() == F841_SRC  # no --apply
    assert t.roles() == [("triage", "ruff:F841"), ("fixer", "ruff:F841")]
    # Triage runs with JSON mode; the fixer doesn't. Reasoning is off by default.
    assert [c.json_mode for c in t.calls] == [True, False]
    assert not any(c.reasoning for c in t.calls)
    [row] = store.rows("findings", res.run_id)
    assert (row["status"], row["triage_decision"], row["final_decision"]) == (
        "fixed",
        "auto_fix",
        "auto_fix",
    )


def test_suggestion_is_reported_not_patched(tmp_path):
    fixed = "def add(x, xs=None):\n    if xs is None:\n        xs = []\n    xs.append(x)\n"
    res, t, store, _ = fix(
        tmp_path,
        {"app/mod.py": B006_SRC},
        {
            ("triage", "ruff:B006"): triage_json("auto_fix"),  # clamped: not on the allowlist
            ("fixer", "ruff:B006"): fixer_reply(
                "app/mod.py", "def add(x, xs=[]):\n    xs.append(x)\n", fixed
            ),
        },
    )
    o = only(res)
    assert o.status == FindingStatus.suggested
    assert o.clamp.clamped and o.clamp.decision == "suggest"
    assert "+    if xs is None:" in o.diff  # B006 may change a default (signature_rules)
    assert res.patch == ""
    [row] = store.rows("findings", res.run_id)
    assert row["clamped"] == 1


@pytest.mark.parametrize(
    ("files", "rule", "decision", "status"),
    [
        ({"app/auth/keys.py": "import json\n"}, "ruff:F401", "auto_fix", "escalated"),
        ({"app/mod.py": B006_SRC}, "ruff:B006", "false_positive", "false_positive"),
        ({"app/mod.py": B006_SRC}, "ruff:B006", "escalate", "escalated"),
    ],
)
def test_no_fixer_call(tmp_path, files, rule, decision, status):
    res, t, _, _ = fix(tmp_path, files, {("triage", rule): triage_json(decision)})
    assert only(res).status == status
    assert t.roles() == [("triage", rule)]


def test_protected_path_is_clamped_to_escalate(tmp_path):
    res, _, _, _ = fix(
        tmp_path,
        {"app/auth/keys.py": "import json\n"},
        {("triage", "ruff:F401"): triage_json("auto_fix")},
    )
    o = only(res)
    assert o.status == FindingStatus.escalated and o.clamp.clamped
    assert "protected path" in o.reason
    assert res.needs_human and res.outcome == "escalations"


def test_false_positive_on_allowlisted_rule_becomes_suggest(tmp_path):
    res, t, _, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {
            ("triage", "ruff:F841"): triage_json("false_positive"),
            ("fixer", "ruff:F841"): fixer_reply("app/mod.py", "    unused = 0\n", ""),
        },
    )
    o = only(res)
    assert o.clamp.decision == "suggest" and o.status == FindingStatus.suggested


def test_edit_retry_with_feedback(tmp_path):
    res, t, store, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): [
                fixer_reply("app/mod.py", "    unused = 1\n", ""),
                fixer_reply("app/mod.py", "    unused = 0\n", ""),
            ],
        },
    )
    o = only(res)
    assert o.status == FindingStatus.fixed and o.fix_rounds == 2
    retry = t.calls[2].messages
    assert "no_match" in retry[-1]["content"] and retry[-2]["role"] == "assistant"
    errors = [s["error"] for s in store.rows("steps", res.run_id) if s["node"] == "apply_edits"]
    assert errors[0].startswith("edit_apply_failed: no_match") and errors[1] is None


def test_edit_fails_twice(tmp_path):
    res, t, _, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): ["I can't do that.", "Still no blocks."],
        },
    )
    o = only(res)
    assert o.status == FindingStatus.failed and o.fix_rounds == 2
    assert "no SEARCH/REPLACE" in o.reason
    assert res.patch == "" and res.needs_human


def test_suppression_is_rejected_and_restored(tmp_path):
    res, _, store, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): fixer_reply(
                "app/mod.py", "    unused = 0\n", "    unused = 0  # noqa: F841\n"
            ),
        },
    )
    o = only(res)
    assert o.status == FindingStatus.escalated
    assert [v.kind for v in o.violations] == ["suppression_added"]
    assert res.patch == ""
    [v] = store.rows("violations", res.run_id)
    assert v["kind"] == "suppression_added"


def test_editing_a_test_file_is_rejected(tmp_path):
    test_src = "from app.mod import f\n\n\ndef test_f():\n    assert f() == 1\n"
    res, _, _, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC, "tests/test_mod.py": test_src},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): fixer_reply("app/mod.py", "    unused = 0\n", "")
            + fixer_reply("tests/test_mod.py", "    assert f() == 1\n", "    pass\n"),
        },
    )
    o = only(res)
    assert o.status == FindingStatus.escalated
    assert {v.kind for v in o.violations} == {"test_file_touched", "other_file_touched"}
    assert res.patch == ""


def test_invalid_triage_json_escalates(tmp_path):
    res, t, store, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {("triage", "ruff:F841"): ["not json", '{"decision": "maybe"}']},
    )
    o = only(res)
    assert o.status == FindingStatus.escalated
    assert "invalid_json_from_llm" in o.reason
    llm = [s for s in store.rows("steps", res.run_id) if s["node"] == "llm:triage"]
    assert llm[0]["error"].startswith("LLMError: invalid_json_from_llm")


def test_fixer_api_error_escalates(tmp_path):
    res, _, _, _ = fix(
        tmp_path,
        {"app/mod.py": F841_SRC},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): LLMError("api_error", "HTTP 400: bad model"),
        },
    )
    o = only(res)
    assert o.status == FindingStatus.escalated and "fixer failed" in o.reason


def test_budget_exceeded_escalates_the_rest(tmp_path):
    src = F841_SRC + "\n\ndef g():\n    other = 0\n    return 2\n"
    expensive = RawReply(triage_json("escalate"), "stop", "m", cost=0.3)
    res, t, store, _ = fix(
        tmp_path,
        {"app/mod.py": src},
        {("triage", "ruff:F841"): [expensive, expensive]},
        cfg=Config(budget_usd=0.25),
    )
    assert [o.status for o in res.outcomes] == ["escalated", "escalated"]
    # Bottom-up: g's finding was triaged first; f's was never sent.
    assert len(t.calls) == 1
    assert "budget_exceeded" in res.outcomes[0].reason
    assert res.outcome == "budget_exceeded"
    [run] = store.rows("runs", res.run_id)
    assert run["outcome"] == "budget_exceeded" and run["cost_usd"] == pytest.approx(0.3)
