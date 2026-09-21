"""The deterministic pass on the fixture repo. Runs the real ruff (no network)."""

import shutil
from pathlib import Path

import pytest

from patchwarden.analyzers.ruff import RuffAnalyzer
from patchwarden.config import load_config
from patchwarden.deterministic import run_deterministic
from patchwarden.models import Finding, Region
from patchwarden.scan import scan
from patchwarden.workspace import open_workspace

REPO = Path(__file__).parent / "fixtures/repo_small"


@pytest.fixture(scope="module")
def scanned():
    return scan(REPO)[0], load_config(REPO)


def resolved_rules(result, fps):
    by_fp = {f.fingerprint: f for f in result.findings}
    return sorted((by_fp[fp].rule_id, by_fp[fp].location) for fp in fps)


def test_ruff_fixes_only_allowlisted_findings(scanned):
    result, cfg = scanned
    before = {p: p.read_bytes() for p in REPO.rglob("*.py")}
    with open_workspace(REPO) as ws:
        det = run_deterministic(ws, result, cfg)
        assert resolved_rules(result, det.resolved) == [
            ("ruff:F401", "app/utils.py:1"),
            ("ruff:F401", "app/utils.py:2"),
            ("ruff:UP004", "app/shapes.py:1"),
            ("ruff:UP006", "app/utils.py:6"),
            ("ruff:UP032", "app/shapes.py:9"),
            ("ruff:UP035", "app/utils.py:3"),
        ]
        assert det.fixed_files == ["app/shapes.py", "app/utils.py"]
        assert det.reverted == {}
        # The unused import in the protected auth/ file is left for a human.
        assert ws.changed_files() == ["app/shapes.py", "app/utils.py"]
        utils = ws.read("app/utils.py")
        # Only unsafe fixes exist for these; they stay for the Fixer.
        assert "unused = 0" in utils and "value == None" in utils
        assert "bucket=[]" in utils
    assert {p: p.read_bytes() for p in REPO.rglob("*.py")} == before


class NewFindingAnalyzer:
    """Real ruff, plus a made-up finding in utils.py, as if the fix had introduced it."""

    name = "ruff"

    def available(self):
        return None

    def run(self, repo, files):
        found = RuffAnalyzer(["E", "F", "B", "UP", "SIM"]).run(repo, files)
        return [
            *found,
            Finding(
                tool="ruff",
                rule_id="ruff:F999",
                message="new",
                file="app/utils.py",
                region=Region(start_line=1, end_line=1),
                snippet="x",
                fingerprint="brand-new",
            ),
        ]


def test_file_with_new_finding_is_reverted(scanned):
    result, cfg = scanned
    with open_workspace(REPO) as ws:
        det = run_deterministic(ws, result, cfg, analyzers=[NewFindingAnalyzer()])
        assert det.fixed_files == ["app/shapes.py"]
        assert "new finding" in det.reverted["app/utils.py"]
        assert ws.changed_files() == ["app/shapes.py"]


def test_nothing_to_fix(tmp_path):
    shutil.copytree(REPO / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "app/utils.py").unlink()
    (tmp_path / "app/shapes.py").unlink()
    result, _ = scan(tmp_path)
    with open_workspace(tmp_path) as ws:
        det = run_deterministic(ws, result, load_config(tmp_path))
        assert det.resolved == [] and ws.changes() == {}
