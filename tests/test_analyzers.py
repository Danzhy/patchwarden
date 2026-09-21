"""Analyzer subprocess handling, with small stand-in executables instead of the real tools."""

import shutil
import sys
from pathlib import Path

import pytest

from patchwarden.agents.reporter import render_scan_text
from patchwarden.analyzers.base import AnalyzerError, run_sarif_tool
from patchwarden.analyzers.codeql import CodeQLAnalyzer
from patchwarden.config import Config
from patchwarden.scan import scan

FIXTURES = Path(__file__).parent / "fixtures"


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_exit_1_means_findings(tmp_path):
    doc = run_sarif_tool("t", py("print('{\"runs\": []}'); raise SystemExit(1)"), tmp_path)
    assert doc == {"runs": []}


def test_other_exit_code_is_an_error(tmp_path):
    with pytest.raises(AnalyzerError, match="exited 2: bad flag"):
        run_sarif_tool("t", py("import sys; sys.stderr.write('bad flag'); sys.exit(2)"), tmp_path)


def test_invalid_sarif_is_an_error(tmp_path):
    with pytest.raises(AnalyzerError, match="invalid SARIF"):
        run_sarif_tool("t", py("print('not json')"), tmp_path)


def test_timeout_is_an_error(tmp_path):
    with pytest.raises(AnalyzerError, match="timed out"):
        run_sarif_tool("t", py("import time; time.sleep(5)"), tmp_path, timeout=0.2)


FAKE_CODEQL = """#!{python}
import shutil, sys
args = sys.argv[1:]
if args[:2] == ["database", "analyze"]:
    out = next(a for a in args if a.startswith("--output=")).split("=", 1)[1]
    shutil.copy({sarif!r}, out)
sys.exit({code})
"""


def fake_codeql(tmp_path, monkeypatch, code: int = 0) -> None:
    exe = tmp_path / "codeql"
    sarif = str(FIXTURES / "sarif/codeql.sarif")
    exe.write_text(FAKE_CODEQL.format(python=sys.executable, sarif=sarif, code=code))
    exe.chmod(0o755)
    monkeypatch.setenv("CODEQL", str(exe))


def test_codeql_runs_and_filters_to_requested_files(tmp_path, monkeypatch):
    fake_codeql(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "repo_small", repo)
    a = CodeQLAnalyzer()
    assert a.available() is None
    found = a.run(repo, ["app/utils.py"])
    assert [f.rule_id for f in found] == ["codeql:py/unused-import"]
    assert a.run(repo, ["app/shapes.py"]) == []


def test_codeql_failure_is_an_error(tmp_path, monkeypatch):
    fake_codeql(tmp_path, monkeypatch, code=3)
    with pytest.raises(AnalyzerError, match="database create exited 3"):
        CodeQLAnalyzer().run(FIXTURES / "repo_small", ["app/utils.py"])


def test_empty_repo_report(tmp_path):
    result, _ = scan(tmp_path, cfg=Config())
    assert result.findings == []
    assert "No findings." in render_scan_text(result)
