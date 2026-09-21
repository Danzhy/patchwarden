"""`trace list | show | stats` over runs made by `fix` with a scripted LLM."""

import json
import shlex
import sys

import pytest
from fake_llm import FakeTransport, fake_client, fixer_reply, triage_json, verifier_json
from typer.testing import CliRunner

from patchwarden.cli import app

runner = CliRunner()
SRC = "def f():\n    unused = 0\n    return 1\n"


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app/mod.py").write_text(SRC)
    cmd = f"{shlex.quote(sys.executable)} -c pass"
    (repo / "pyproject.toml").write_text(f"[tool.patchwarden]\ntest_command = {json.dumps(cmd)}\n")
    return repo


def run_fix(tmp_path, repo, monkeypatch, fixer):
    script = {
        ("triage", "ruff:F841"): triage_json("auto_fix"),
        ("fixer", "ruff:F841"): fixer,
        ("verifier", "*"): verifier_json(),
    }
    transport = FakeTransport(script)
    monkeypatch.setattr(
        "patchwarden.cli.make_llm", lambda cfg, run: fake_client(transport, cfg, run)
    )
    return runner.invoke(app, ["fix", str(repo), "--output", str(tmp_path / "p.patch")])


def test_cheating_fix_is_flagged_and_shown(tmp_path, repo, monkeypatch):
    noqa = fixer_reply("app/mod.py", "    unused = 0\n", "    unused = 0  # noqa: F841\n")
    res = run_fix(tmp_path, repo, monkeypatch, noqa)
    assert res.exit_code == 1, res.output
    assert "flags: cheating_attempt x1 (patchwarden trace show " in res.output

    show = runner.invoke(app, ["trace", "show", "last"])
    assert show.exit_code == 0, show.output
    out = show.output
    assert "finding  ruff:F841 app/mod.py:2  -> escalated" in out
    assert "check_diff  VIOLATION suppression_added" in out
    assert "llm:triage  deepseek/" in out  # nested under its triage step
    assert "Flags (1):" in out
    assert "cheating_attempt  ruff:F841 app/mod.py:2  suppression_added: app/mod.py: adds" in out


def test_list_show_by_prefix_and_stats(tmp_path, repo, monkeypatch):
    good = fixer_reply("app/mod.py", "    unused = 0\n", "")
    assert run_fix(tmp_path, repo, monkeypatch, good).exit_code == 0

    listing = runner.invoke(app, ["trace", "list"])
    assert listing.exit_code == 0
    [header, row] = listing.output.splitlines()
    run_id = row.split()[0]
    assert header.startswith("run ") and " ok " in row and row.endswith("$0.0030")

    show = runner.invoke(app, ["trace", "show", run_id[:17]])
    assert show.exit_code == 0 and f"run {run_id}" in show.output
    assert "verify  passed (tests passed)" in show.output
    assert "verifier  pass, low risk: scripted" in show.output
    assert "Flags (0)" in show.output

    stats = runner.invoke(app, ["trace", "stats"])
    assert stats.exit_code == 0
    assert "1 run(s), 1 findings, LLM cost $0.0030" in stats.output
    [line] = [ln for ln in stats.output.splitlines() if ln.startswith("ruff:F841")]
    assert line.split() == ["ruff:F841", "1", "1", "0", "0", "0", "0", "100%", "$0.0030"]
    assert runner.invoke(app, ["trace", "stats", "--run", "last"]).exit_code == 0


def test_trace_errors(tmp_path, repo, monkeypatch):
    missing = runner.invoke(app, ["trace", "list", "--trace-dir", str(tmp_path / "none")])
    assert missing.exit_code == 2 and "no trace store" in missing.output
    assert not (tmp_path / "none").exists()  # reading never creates a store
    run_fix(tmp_path, repo, monkeypatch, fixer_reply("app/mod.py", "    unused = 0\n", ""))
    for args in (["show", "nope"], ["stats", "--run", "nope"]):
        res = runner.invoke(app, ["trace", *args])
        assert res.exit_code == 2 and "no run matches" in res.output
