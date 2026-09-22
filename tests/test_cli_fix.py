"""`fix` end to end on the fixture repo with a scripted LLM. Real ruff and bandit, no network."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fake_llm import FakeTransport, fake_client, fixer_reply, triage_json, verifier_json
from typer.testing import CliRunner

from patchwarden.cli import app
from patchwarden.scan import scan

REPO = Path(__file__).parent / "fixtures/repo_small"
runner = CliRunner()

B006_FIX = (
    "def append_item(item, bucket=None):\n    if bucket is None:\n        bucket = []\n"
    "    bucket.append(item)\n"
)
# Findings are fixed bottom-up per file, so each SEARCH text is the file as the Fixer sees it.
SCRIPT = {
    ("triage", "*"): triage_json("escalate", "security", "use hashlib.sha256 / no shell=True"),
    ("triage", "ruff:F401"): triage_json("auto_fix", "unused import"),  # protected path
    ("triage", "ruff:F841"): triage_json("auto_fix", "unused variable"),
    ("triage", "ruff:E711"): triage_json("auto_fix", "use is None"),
    ("triage", "ruff:B006"): triage_json("suggest", "changes the default"),
    ("triage", "ruff:SIM103"): triage_json("suggest", "simplify"),
    ("fixer", "ruff:B006"): fixer_reply(
        "app/utils.py", "def append_item(item, bucket=[]):\n    bucket.append(item)\n", B006_FIX
    ),
    ("fixer", "ruff:E711"): fixer_reply(
        "app/utils.py", "    if value == None:\n", "    if value is None:\n"
    ),
    ("fixer", "ruff:SIM103"): [
        fixer_reply(
            "app/shapes.py",
            "    if sq.area() > 100:\n        return True\n    else:\n        return False\n",
            "    return sq.area() > 100\n",
        ),
        fixer_reply(
            "app/utils.py",
            "    if value is None:\n        return True\n    return False\n",
            "    return value is None\n",
        ),
    ],
    ("fixer", "ruff:F841"): fixer_reply("app/utils.py", "    unused = 0\n", "", "drop it"),
    ("verifier", "*"): verifier_json(),
}


def copy_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns("__pycache__"))
    return repo


def snapshot(repo):
    return {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.fixture
def fake(monkeypatch):
    transport = FakeTransport(SCRIPT)
    monkeypatch.setattr(
        "patchwarden.cli.make_llm", lambda cfg, run: fake_client(transport, cfg, run)
    )
    return transport


def invoke(tmp_path, repo, *extra):
    return runner.invoke(
        app,
        [
            "fix",
            str(repo),
            "--output",
            str(tmp_path / "out.patch"),
            "--report",
            str(tmp_path / "report.md"),
            "--trace-dir",
            str(tmp_path / "trace"),
            *extra,
        ],
    )


def test_in_github_actions(tmp_path, fake, monkeypatch):
    """trigger=ci, and a patch outside the report's directory is named by its path."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    repo = copy_repo(tmp_path)
    patch = tmp_path / "elsewhere" / "fixes.patch"
    res = invoke(tmp_path, repo, "--output", str(patch))
    assert res.exit_code == 1, res.output
    assert f"All of these are in `{patch}`." in (tmp_path / "report.md").read_text()
    [run] = db_rows(tmp_path, "SELECT trigger FROM runs")
    assert run["trigger"] == "ci"


def db_rows(tmp_path, sql):
    db = sqlite3.connect(tmp_path / "trace/traces.db")
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(sql)]


def test_full_fix_with_fake_llm(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    repo = copy_repo(tmp_path)
    before = snapshot(repo)
    res = invoke(tmp_path, repo)
    assert res.exit_code == 1, res.output  # escalations
    assert (
        "ruff fixed 6 of 8 candidate findings; Fixer fixed 2; suggested 3; escalated 4; "
        "false positive 0." in res.output
    )
    assert snapshot(repo) == before

    patch = (tmp_path / "out.patch").read_text()
    assert "-    unused = 0" in patch and "+    if value is None:" in patch
    assert "-import os" in patch and "+class Square:" in patch
    assert "bucket=None" not in patch and "return sq.area() > 100" not in patch
    assert "auth/tokens.py" not in patch

    report = (tmp_path / "report.md").read_text()
    assert "All of these are in `out.patch`." in report  # next to the report: its name
    suggested = report.split("## Suggested")[1].split("## Escalated")[0]
    assert suggested.count("**ruff:") == 3 and "+def append_item(item, bucket=None):" in suggested
    # SIM103 in utils.py was refreshed from the re-scan after the E711 fix above it.
    assert "Return the condition `value is None` directly" in suggested
    assert suggested.count("Checks: re-scan clean; tests passed; Verifier pass (low risk)") == 3
    assert "the tests (`python -m pytest -q`)" in report
    escalated = report.split("## Escalated (4)")[1]
    assert "bandit:B602" in escalated and "ruff:F401" in escalated
    assert "protected path" in escalated
    assert "Proposed approach / risks: use hashlib.sha256" in escalated

    # Triage for all 9 findings left after ruff; the Fixer and the Verifier for the 5
    # auto_fix/suggest ones (every fix passed its checks on the first round).
    roles = [role for role, _ in fake.roles()]
    assert (roles.count("triage"), roles.count("fixer"), roles.count("verifier")) == (9, 5, 5)

    [run] = db_rows(tmp_path, "SELECT * FROM runs")
    assert run["outcome"] == "escalations" and run["trigger"] == "cli"
    assert run["cost_usd"] == pytest.approx(0.019) and run["prompt_version"].startswith("m4.")
    tests = db_rows(tmp_path, "SELECT input_json, output_json FROM steps WHERE node = 'tests'")
    assert [json.loads(t["input_json"])["stage"] for t in tests] == ["baseline", "deterministic"]
    assert all(json.loads(t["output_json"])["status"] == "passed" for t in tests)
    verify = db_rows(tmp_path, "SELECT output_json FROM steps WHERE node = 'verify'")
    assert [json.loads(v["output_json"])["tests"] for v in verify] == ["passed"] * 5
    # Only the protected-path F401: Triage said auto_fix, policy escalated it.
    flags = db_rows(tmp_path, "SELECT detector FROM flags")
    assert [f["detector"] for f in flags] == ["triage_policy_disagreement"]
    findings = db_rows(tmp_path, "SELECT * FROM findings")
    assert len(findings) == 15
    status = {}
    for f in findings:
        status[f["status"]] = status.get(f["status"], 0) + 1
    assert status == {"fixed": 8, "suggested": 3, "escalated": 4}
    [prot] = [
        f for f in findings if f["file"] == "app/auth/tokens.py" and f["rule_id"] == "ruff:F401"
    ]
    assert (prot["triage_decision"], prot["final_decision"], prot["clamped"]) == (
        "auto_fix",
        "escalate",
        1,
    )
    steps = db_rows(tmp_path, "SELECT node, parent_step_id FROM steps")
    assert sum(1 for s in steps if s["node"] == "triage") == 9
    assert all(s["parent_step_id"] for s in steps if s["node"].startswith("llm:"))
    jsonl = next((tmp_path / "trace/runs").glob("*.jsonl")).read_text().splitlines()
    assert sum(1 for ln in jsonl if json.loads(ln)["table"] == "findings") == 15


def test_apply_writes_only_auto_fixes(tmp_path, fake):
    repo = copy_repo(tmp_path)
    res = invoke(tmp_path, repo, "--apply")
    assert res.exit_code == 1, res.output
    assert "applied to 2 files" in res.output
    utils = (repo / "app/utils.py").read_text()
    assert "unused" not in utils and "value is None" in utils and "bucket=[]" in utils
    # 15 - 6 (ruff) - 2 (Fixer) = 7 left: the suggestions and escalations.
    assert len(scan(repo)[0].findings) == 7


def test_no_llm_keeps_the_deterministic_pass(tmp_path, monkeypatch):
    def no_llm(cfg, run):
        raise AssertionError("--no-llm must not create an LLM client")

    monkeypatch.setattr("patchwarden.cli.make_llm", no_llm)
    repo = copy_repo(tmp_path)
    res = invoke(tmp_path, repo, "--no-llm")
    assert res.exit_code == 1, res.output  # the bandit/protected findings still escalate
    assert "ruff fixed 6 of 8 candidate findings" in res.output
    assert "escalated 4" in res.output and "not triaged 5" in res.output
    patch = (tmp_path / "out.patch").read_text()
    assert "+++ b/app/utils.py" in patch and "+++ b/app/shapes.py" in patch
    assert "-    unused = 0" not in patch  # F841 needs the Fixer
    assert "## Not triaged (5)" in (tmp_path / "report.md").read_text()


def test_missing_api_key_exits_2(tmp_path):
    res = invoke(tmp_path, copy_repo(tmp_path))
    assert res.exit_code == 2
    assert "no_api_key" in res.output and "--no-llm" in res.output
    [run] = db_rows(tmp_path, "SELECT outcome FROM runs")
    assert run["outcome"] == "error"


def test_clean_repo_exits_0_without_llm(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("def f(x: int) -> int:\n    return x\n")
    res = invoke(tmp_path, repo)  # no key needed: nothing left for the LLM
    assert res.exit_code == 0, res.output
    assert "patch: " in res.output and "(empty)" in res.output


def test_default_outputs_go_to_cwd(tmp_path, monkeypatch):
    repo = copy_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    res = runner.invoke(app, ["fix", str(repo), "--no-llm"])
    assert res.exit_code == 1, res.output
    assert (work / "patchwarden.patch").is_file() and (work / "patchwarden-report.md").is_file()
    assert (work / ".patchwarden/traces.db").is_file()
    assert not (repo / ".patchwarden").exists()


def test_budget_option(tmp_path, fake):
    res = invoke(tmp_path, copy_repo(tmp_path), "--budget-usd", "0.002")
    assert res.exit_code == 1
    assert "budget_exceeded" in res.output
    assert len(fake.calls) == 2


def test_fix_bad_config_exits_2(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.patchwarden]\nnope = 1\n")
    res = runner.invoke(app, ["fix", str(tmp_path), "--output", str(tmp_path / "p")])
    assert res.exit_code == 2


def test_outputs_in_missing_directories_are_created(tmp_path):
    res = runner.invoke(
        app,
        [
            "fix",
            str(copy_repo(tmp_path)),
            "--no-llm",
            "--output",
            "out/a/p.patch",
            "--report",
            "out/b/r.md",
        ],
    )
    assert res.exit_code == 1, res.output
    assert (tmp_path / "out/a/p.patch").is_file() and (tmp_path / "out/b/r.md").is_file()
