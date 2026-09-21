"""`scan` end to end on the fixture repo. Runs the real ruff and bandit (no network)."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from patchwarden.analyzers import AnalyzerError
from patchwarden.analyzers.codeql import CodeQLAnalyzer
from patchwarden.cli import app
from patchwarden.config import Config
from patchwarden.scan import scan

REPO = Path(__file__).parent / "fixtures/repo_small"
runner = CliRunner()


@pytest.fixture(scope="module")
def result():
    return scan(REPO)[0]


def kinds(result) -> dict[tuple[str, str, int], str]:
    return {
        (f.rule_id, f.file, f.region.start_line): result.preclass[f.fingerprint].kind
        for f in result.findings
    }


def test_scan_counts(result):
    assert len(result.findings) == 15
    assert result.analyzers_run == ["ruff", "bandit"]
    assert result.scope_mode == "full"
    # Bandit skips test files (B101 would flag every assert).
    assert not any(f.file.startswith("tests/") for f in result.findings)
    assert not any(f.file == "app/clean.py" for f in result.findings)
    assert len({f.fingerprint for f in result.findings}) == 15


def test_scan_preclassification(result):
    k = kinds(result)
    assert k[("bandit:B602", "app/runner.py", 5)] == "always_escalate"
    assert k[("bandit:B324", "app/auth/tokens.py", 6)] == "always_escalate"
    assert k[("ruff:F401", "app/auth/tokens.py", 2)] == "protected_path"
    assert k[("ruff:F401", "app/utils.py", 1)] == "auto_fix_allowed"
    assert k[("ruff:B006", "app/utils.py", 17)] == "llm_decides"
    counts = {v: list(k.values()).count(v) for v in set(k.values())}
    assert counts == {
        "always_escalate": 3,
        "protected_path": 1,
        "auto_fix_allowed": 8,
        "llm_decides": 3,
    }


def test_scan_does_not_modify_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns("__pycache__"))
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    scan(repo)
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert before == after


def test_cli_text_report():
    res = runner.invoke(app, ["scan", str(REPO)])
    assert res.exit_code == 0, res.output
    assert "15 findings" in res.output
    assert "escalate: 3, protected: 1, auto-fix: 8, triage: 3" in res.output
    assert "app/runner.py:5" in res.output


def test_cli_json_report(tmp_path):
    out = tmp_path / "scan.json"
    res = runner.invoke(app, ["scan", str(REPO), "--format", "json", "--output", str(out)])
    assert res.exit_code == 0, res.output
    data = json.loads(out.read_text())
    assert len(data["findings"]) == 15
    assert set(data["preclass"]) == {f["fingerprint"] for f in data["findings"]}


def test_cli_bad_config_exits_2(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.patchwarden]\nanalyzer = ['ruff']\n")
    res = runner.invoke(app, ["scan", str(tmp_path)])
    assert res.exit_code == 2
    assert "unknown [tool.patchwarden] keys: analyzer" in res.output


def test_cli_unknown_analyzer_exits_2(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.patchwarden]\nanalyzers = ['pylint']\n")
    res = runner.invoke(app, ["scan", str(tmp_path)])
    assert res.exit_code == 2
    assert "unknown analyzers: pylint" in res.output


def test_codeql_missing_binary_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEQL", str(tmp_path / "no-such-codeql"))
    cfg = Config(analyzers=["ruff", "codeql"])
    result, warnings = scan(REPO, cfg=cfg)
    assert result.analyzers_run == ["ruff"]
    assert "codeql" in result.analyzers_skipped
    assert any("codeql skipped" in w for w in warnings)
    assert CodeQLAnalyzer().available() is not None


class BrokenAnalyzer:
    name = "broken"

    def available(self):
        return None

    def run(self, repo, files):
        raise AnalyzerError("broken exited 2: boom")


def test_analyzer_error_propagates():
    with pytest.raises(AnalyzerError, match="boom"):
        scan(REPO, analyzers=[BrokenAnalyzer()])
