"""The per-finding graph (Triage -> clamp -> Fixer -> apply -> check_diff -> verify ->
Verifier), driven through run_fix on tiny repos with a scripted LLM. Real ruff and bandit, no
network. Unless a test says otherwise the repo's "tests" pass and the Verifier passes a fix."""

import dataclasses
import shlex
import sys

import pytest
from fake_llm import FakeTransport, fake_client, fixer_reply, triage_json, verifier_json

from patchwarden.config import Config
from patchwarden.llm import LLMError, RawReply
from patchwarden.models import FindingStatus
from patchwarden.pipeline import run_fix
from patchwarden.tracing.store import TraceStore

TESTED = Config(test_command=f"{shlex.quote(sys.executable)} -c pass")
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
    transport = FakeTransport({("verifier", "*"): verifier_json(), **script})
    store = TraceStore(tmp_path / "trace")
    res = run_fix(
        repo,
        cfg or TESTED,
        store=store,
        llm_factory=lambda c, run: fake_client(transport, c, run),
        **kw,
    )
    return res, transport, store, repo


def flags(store, res):
    return [f["detector"] for f in store.rows("flags", res.run_id)]


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
    assert [role for role, _ in t.roles()] == ["triage", "fixer", "verifier"]
    # Triage and the Verifier reply in JSON; the Fixer doesn't. Reasoning is off by default.
    assert [c.json_mode for c in t.calls] == [True, False, True]
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
    res, t, store, _ = fix(
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
    assert flags(store, res) == ["round_limit_hit", "edit_apply_failed", "edit_apply_failed"]


def test_suppression_is_rejected_and_restored(tmp_path):
    res, t, store, _ = fix(
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
    # A cheating attempt isn't retried, and never reaches the Verifier.
    assert [role for role, _ in t.roles()] == ["triage", "fixer"]
    assert flags(store, res) == ["cheating_attempt"]


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
        cfg=dataclasses.replace(TESTED, budget_usd=0.25),
    )
    assert [o.status for o in res.outcomes] == ["escalated", "escalated"]
    # Bottom-up: g's finding was triaged first; f's was never sent.
    assert len(t.calls) == 1
    assert "budget_exceeded" in res.outcomes[0].reason
    assert res.outcome == "budget_exceeded"
    [run] = store.rows("runs", res.run_id)
    assert run["outcome"] == "budget_exceeded" and run["cost_usd"] == pytest.approx(0.3)


def test_edit_above_later_findings_shifts_their_lines(tmp_path):
    src = F841_SRC + "\n\ndef g():\n    other = 0\n    return 2\n"
    res, t, _, _ = fix(
        tmp_path,
        {"app/mod.py": src},
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): [
                # g's fix (bottom-up, so first) also adds a line above f's finding.
                fixer_reply("app/mod.py", "def f():\n", "# helpers\ndef f():\n")
                + fixer_reply("app/mod.py", "    other = 0\n", ""),
                fixer_reply("app/mod.py", "    unused = 0\n", ""),
            ],
        },
    )
    assert [o.status for o in res.outcomes] == ["fixed", "fixed"]
    f_prompts = [c.messages[1]["content"] for c in t.calls if "unused" in c.messages[1]["content"]]
    # f's finding was on line 2; the inserted comment moved it to 3 for both roles.
    assert all("Location: app/mod.py:3" in p for p in f_prompts[-2:])
    triage_f = [c for c in t.calls if c.role == "triage"][1].messages[1]["content"]
    assert "> 3 |     unused = 0" in triage_f


def test_non_utf8_file_is_escalated_not_a_crash(tmp_path):
    repo = make_repo(tmp_path, {})
    (repo / "app").mkdir(parents=True)
    (repo / "app/m.py").write_bytes(b"# caf\xe9\n" + F841_SRC.encode())
    transport = FakeTransport({})
    res = run_fix(
        repo,
        Config(),
        store=TraceStore(tmp_path / "trace"),
        llm_factory=lambda c, run: fake_client(transport, c, run),
    )
    o = only(res)
    assert o.status == FindingStatus.escalated and "not an editable UTF-8" in o.reason
    assert transport.calls == []


def test_apply_refused_keeps_the_patch(tmp_path):
    repo = make_repo(tmp_path, {"app/mod.py": F841_SRC})
    transport = FakeTransport(
        {
            ("triage", "ruff:F841"): triage_json("auto_fix"),
            ("fixer", "ruff:F841"): fixer_reply("app/mod.py", "    unused = 0\n", ""),
            ("verifier", "*"): verifier_json(),
        }
    )

    def factory(c, run):  # called after the workspace copy: the user edits the file meanwhile
        (repo / "app/mod.py").write_text(F841_SRC + "# edited\n")
        return fake_client(transport, c, run)

    res = run_fix(
        repo, TESTED, store=TraceStore(tmp_path / "trace"), llm_factory=factory, apply=True
    )
    assert "changed since scan" in res.apply_error and res.applied == []
    assert "-    unused = 0" in res.patch
    assert (repo / "app/mod.py").read_text().endswith("# edited\n")


# --- M4: verify, the Verifier and the fix-round loop ---

GOOD = fixer_reply("app/mod.py", "    unused = 0\n", "")
# Removes the unused variable but adds an f-string without placeholders (ruff F541).
NEW_FINDING = fixer_reply("app/mod.py", "    unused = 0\n", '    print(f"done")\n')
AUTO = {("triage", "ruff:F841"): triage_json("auto_fix")}


def test_new_finding_is_retried_then_escalated_at_the_round_limit(tmp_path):
    res, t, store, _ = fix(
        tmp_path, {"app/mod.py": F841_SRC}, {**AUTO, ("fixer", "ruff:F841"): [NEW_FINDING] * 2}
    )
    o = only(res)
    assert o.status == FindingStatus.escalated and o.fix_rounds == 2
    assert o.reason.startswith("no fix passed verification in 2 round(s); last: new finding: ")
    assert "ruff:F541" in o.reason and res.patch == ""
    # The Verifier never sees a fix that failed the code checks.
    assert [role for role, _ in t.roles()] == ["triage", "fixer", "fixer"]
    retry = t.calls[2].messages
    assert retry[-2]["content"] == NEW_FINDING
    assert (
        "That fix was not accepted: new finding: ruff:F541 at app/mod.py:2" in retry[-1]["content"]
    )
    assert sorted(flags(store, res)) == [
        "new_finding_introduced",
        "new_finding_introduced",
        "round_limit_hit",
    ]


def test_second_round_can_succeed(tmp_path):
    res, t, store, _ = fix(
        tmp_path, {"app/mod.py": F841_SRC}, {**AUTO, ("fixer", "ruff:F841"): [NEW_FINDING, GOOD]}
    )
    o = only(res)
    assert o.status == FindingStatus.fixed and o.fix_rounds == 2
    assert "-    unused = 0" in res.patch and "print" not in res.patch
    assert flags(store, res) == ["new_finding_introduced"]


def test_max_fix_rounds_is_configurable(tmp_path):
    cfg = dataclasses.replace(TESTED, max_fix_rounds=3)
    script = {**AUTO, ("fixer", "ruff:F841"): [NEW_FINDING, NEW_FINDING, GOOD]}
    res, _, _, _ = fix(tmp_path, {"app/mod.py": F841_SRC}, script, cfg=cfg)
    assert only(res).status == FindingStatus.fixed and only(res).fix_rounds == 3


def test_syntax_error_is_retried_not_a_violation(tmp_path):
    broken = fixer_reply("app/mod.py", "    unused = 0\n    return 1\n", "    return (1\n")
    res, t, store, _ = fix(
        tmp_path, {"app/mod.py": F841_SRC}, {**AUTO, ("fixer", "ruff:F841"): [broken, GOOD]}
    )
    assert only(res).status == FindingStatus.fixed
    assert "doesn't parse" in t.calls[2].messages[-1]["content"]
    assert store.rows("violations", res.run_id) == []


def test_verifier_rejection_is_fed_back_to_the_fixer(tmp_path):
    script = {
        **AUTO,
        ("fixer", "ruff:F841"): [GOOD, GOOD],
        ("verifier", "ruff:F841"): [
            verifier_json("fail", reason="drops a side effect"),
            verifier_json(),
        ],
    }
    res, t, _, _ = fix(tmp_path, {"app/mod.py": F841_SRC}, script)
    o = only(res)
    assert o.status == FindingStatus.fixed and o.fix_rounds == 2
    fixer_calls = [c for c in t.calls if c.role == "fixer"]
    assert (
        "an independent review rejected it: drops a side effect"
        in (fixer_calls[1].messages[-1]["content"])
    )
    # The Verifier gets the diff and the checks, not the Fixer's rationale.
    review = [c for c in t.calls if c.role == "verifier"][0].messages[1]["content"]
    assert "-    unused = 0" in review and "tests pass" in review
    assert "scripted fix" not in review and "RATIONALE" not in review


def test_high_risk_pass_becomes_a_suggestion(tmp_path):
    script = {
        **AUTO,
        ("fixer", "ruff:F841"): GOOD,
        ("verifier", "ruff:F841"): verifier_json("pass", "high", "callers may rely on it"),
    }
    res, _, _, repo = fix(tmp_path, {"app/mod.py": F841_SRC}, script)
    o = only(res)
    assert o.status == FindingStatus.suggested and res.patch == ""
    assert "behaviour-change risk high: callers may rely on it" in o.reason
    assert "-    unused = 0" in o.diff


def test_untested_auto_fix_is_only_suggested(tmp_path):
    script = {**AUTO, ("fixer", "ruff:F841"): GOOD}
    res, t, _, _ = fix(tmp_path, {"app/mod.py": F841_SRC}, script, cfg=Config())
    o = only(res)
    assert o.status == FindingStatus.suggested and res.patch == ""
    assert o.reason == "untested, so not applied automatically: no test_command configured"
    assert o.verify.tests == "skipped"
    review = [c for c in t.calls if c.role == "verifier"][0].messages[1]["content"]
    assert "The tests were not run (no test_command configured)" in review


def test_failing_tests_are_retried_then_escalated(tmp_path):
    check = "from app.mod import f; assert f() == 1"
    cfg = Config(test_command=f"{shlex.quote(sys.executable)} -c {shlex.quote(check)}")
    breaks = fixer_reply("app/mod.py", "    unused = 0\n    return 1\n", "    return 2\n")
    script = {**AUTO, ("fixer", "ruff:F841"): [breaks, breaks]}
    res, _, store, _ = fix(tmp_path, {"app/mod.py": F841_SRC}, script, cfg=cfg)
    o = only(res)
    assert o.status == FindingStatus.escalated and "tests failed" in o.reason
    assert res.patch == ""
    assert sorted(flags(store, res)) == ["round_limit_hit", "tests_broke", "tests_broke"]


def test_failing_baseline_means_suggestions_only(tmp_path):
    cfg = Config(test_command=f"{shlex.quote(sys.executable)} -c 'raise SystemExit(1)'")
    script = {**AUTO, ("fixer", "ruff:F841"): GOOD}
    res, _, _, _ = fix(tmp_path, {"app/mod.py": F841_SRC}, script, cfg=cfg)
    assert only(res).status == FindingStatus.suggested
    assert any("already failed before any change" in w for w in res.warnings)
    assert res.tests_skipped.startswith("the tests already failed")


def test_tests_failing_after_ruff_revert_its_fixes(tmp_path):
    # ruff removes the unused import; this repo's "test" needs it.
    check = (
        "import pathlib, sys; sys.exit('import os' not in pathlib.Path('app/mod.py').read_text())"
    )
    cfg = Config(test_command=f"{shlex.quote(sys.executable)} -c {shlex.quote(check)}")
    src = "import os\n\n\ndef f():\n    return 1\n"
    res, _, store, _ = fix(tmp_path, {"app/mod.py": src}, {}, cfg=cfg, no_llm=True)
    assert res.reverted == {"app/mod.py": "the tests failed after ruff's fixes"}
    assert res.patch == "" and only(res).status == FindingStatus.not_triaged
    assert flags(store, res) == ["tests_broke"]


def test_budget_hit_at_the_verifier_restores_the_file(tmp_path):
    cfg = dataclasses.replace(TESTED, budget_usd=0.002)  # triage + fixer spend it
    res, t, store, _ = fix(
        tmp_path, {"app/mod.py": F841_SRC}, {**AUTO, ("fixer", "ruff:F841"): GOOD}, cfg=cfg
    )
    assert only(res).status == FindingStatus.escalated and res.patch == ""
    assert res.outcome == "budget_exceeded"
    assert [role for role, _ in t.roles()] == ["triage", "fixer"]
    assert flags(store, res) == ["cost_over_budget"]


def test_verifier_invalid_json_escalates(tmp_path):
    script = {**AUTO, ("fixer", "ruff:F841"): GOOD, ("verifier", "ruff:F841"): ["no", "nope"]}
    res, _, store, _ = fix(tmp_path, {"app/mod.py": F841_SRC}, script)
    o = only(res)
    assert o.status == FindingStatus.escalated and o.reason.startswith("verifier failed")
    assert res.patch == ""
    assert flags(store, res) == ["invalid_json_from_llm"]
