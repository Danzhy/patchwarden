"""verify.py: the test runner, and matching findings before/after a fix. Real ruff, no LLM."""

import shlex
import sys

import pytest

from patchwarden.analyzers import run_analyzers
from patchwarden.config import Config
from patchwarden.models import TestStatus
from patchwarden.verify import TestRunner, run_tests, verify_fix
from patchwarden.workspace import open_workspace

PY = shlex.quote(sys.executable)
RUFF_ONLY = Config(analyzers=["ruff"], test_command=f"{PY} -c pass")


def test_run_tests_statuses(tmp_path):
    assert run_tests(tmp_path, f"{PY} -c pass", 30)[0] == TestStatus.passed
    status, detail = run_tests(tmp_path, f"{PY} -c \"print('boom'); raise SystemExit(3)\"", 30)
    assert status == TestStatus.failed and "exited 3" in detail and "boom" in detail
    assert run_tests(tmp_path, f'{PY} -c "import time; time.sleep(5)"', 1)[0] == TestStatus.timeout
    assert run_tests(tmp_path, "no-such-binary-xyz", 5) == (
        TestStatus.error,
        "`no-such-binary-xyz` could not run: ['no-such-binary-xyz'] not found on PATH",
    )
    assert run_tests(tmp_path, "", 5)[0] == TestStatus.error
    assert run_tests(tmp_path, 'unclosed "quote', 5)[0] == TestStatus.error


def test_a_directory_on_path_is_not_the_command(tmp_path, monkeypatch):
    (tmp_path / "bin/python").mkdir(parents=True)  # like CodeQL's tools dir
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    status, detail = run_tests(tmp_path, "python -m pytest", 5)
    assert status == TestStatus.error and "not found on PATH" in detail


def test_tests_get_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("PATCHWARDEN_PLAIN", "kept")
    code = (
        "import os, sys; bad = [k for k in os.environ if 'SECRET' in k or 'TOKEN' in k "
        "or 'OPENROUTER' in k]; sys.exit(1 if bad or 'PATCHWARDEN_PLAIN' not in os.environ else 0)"
    )
    assert run_tests(tmp_path, f"{PY} -c {shlex.quote(code)}", 30)[0] == TestStatus.passed


def test_runner_skips_without_a_command_or_on_a_failing_baseline(tmp_path):
    runner = TestRunner(Config(), tmp_path)
    assert runner.baseline()[0] == TestStatus.skipped
    assert runner.run() == (TestStatus.skipped, "no test_command configured")
    failing = TestRunner(Config(test_command=f"{PY} -c 'raise SystemExit(1)'"), tmp_path)
    assert failing.baseline()[0] == TestStatus.failed
    status, why = failing.run()
    assert status == TestStatus.skipped and "already failed before any change" in why


def check(tmp_path, before: str, after: str, rule: str, nth: int = 0, cfg=RUFF_ONLY):
    """Verify replacing `before` with `after` as the fix for the nth `rule` finding."""
    (tmp_path / "m.py").write_text(before)
    with open_workspace(tmp_path) as ws:
        found, _, _ = run_analyzers(ws.root, ["m.py"], cfg)
        target = [f for f in found if f.rule_id == rule][nth]
        ws.write("m.py", after)
        runner = TestRunner(cfg, ws.root)
        return verify_fix(ws, target, before, found, cfg, runner)


def test_real_fix_passes(tmp_path):
    res, after = check(
        tmp_path, "def f():\n    x = 0\n    return 1\n", "def f():\n    return 1\n", "ruff:F841"
    )
    assert res.passed and res.target_gone and res.tests == TestStatus.passed
    assert after == []


def test_editing_another_findings_line_is_not_a_new_finding(tmp_path):
    # Fixing E711 changes the SIM103 finding's snippet, so its fingerprint, not the finding.
    before = "def f(v):\n    if v == None:\n        return True\n    return False\n"
    after = before.replace("v == None", "v is None")
    res, found = check(tmp_path, before, after, "ruff:E711")
    assert res.passed, res.failures()
    assert [f.rule_id for f in found] == ["ruff:SIM103"]


def test_fixing_one_of_two_identical_findings(tmp_path):
    # Same snippet twice: the second finding's fingerprint has a ":1" suffix, which it loses
    # once the first is fixed. That is neither "target still there" nor "a new finding".
    f, g = (f"def {name}():\n    unused = 0\n    return 1\n\n\n" for name in "fg")

    def drop(text: str) -> str:
        return text.replace("    unused = 0\n", "")

    res, _ = check(tmp_path, f + g, drop(f) + g, "ruff:F841", nth=0)
    assert res.passed, res.failures()
    res, _ = check(tmp_path, f + g, f + drop(g), "ruff:F841", nth=1)
    assert res.passed, res.failures()


def test_new_finding_fails(tmp_path):
    before = "def f():\n    x = 0\n    return 1\n"
    res, _ = check(tmp_path, before, 'def f():\n    print(f"x")\n    return 1\n', "ruff:F841")
    assert not res.passed and res.target_gone
    [new] = res.new_findings
    assert new.startswith("ruff:F541 at m.py:2: ")
    assert res.tests == TestStatus.skipped  # never ran: the re-scan already failed


def test_removing_another_finding_fails(tmp_path):
    before = "import os\n\n\ndef f():\n    x = 0\n    return 1\n"
    after = "\n\ndef f():\n    return 1\n"
    res, _ = check(tmp_path, before, after, "ruff:F841")
    assert res.target_gone and [a.split(" at ")[0] for a in res.also_resolved] == ["ruff:F401"]
    assert "also changes code flagged by another finding" in res.failures()[0]


def test_target_still_reported_fails(tmp_path):
    before = "def f():\n    x = 0\n    return 1\n"
    res, _ = check(tmp_path, before, before.replace("x = 0", "x = 1"), "ruff:F841")
    assert not res.target_gone and res.new_findings == []
    assert res.failures() == ["the analyzer still reports the finding"]


def test_syntax_error_skips_the_rescan(tmp_path):
    res, after = check(tmp_path, "def f():\n    x = 0\n    return 1\n", "def f(:\n", "ruff:F841")
    assert not res.syntax_ok and after is None
    assert res.failures()[0].startswith("the edited file doesn't parse")


@pytest.mark.parametrize(
    ("code", "status"), [("pass", "passed"), ("raise SystemExit(1)", "failed")]
)
def test_tests_run_after_a_clean_rescan(tmp_path, code, status):
    cfg = Config(analyzers=["ruff"], test_command=f"{PY} -c {shlex.quote(code)}")
    before = "def f():\n    x = 0\n    return 1\n"
    res, _ = check(tmp_path, before, "def f():\n    return 1\n", "ruff:F841", cfg=cfg)
    assert res.tests == status and res.passed == (status == "passed")
