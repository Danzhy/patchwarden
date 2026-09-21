"""`fix` end to end (deterministic pass only until M3). Runs the real ruff and bandit."""

import shutil
from pathlib import Path

from typer.testing import CliRunner

from patchwarden.cli import app
from patchwarden.scan import scan

REPO = Path(__file__).parent / "fixtures/repo_small"
runner = CliRunner()


def copy_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns("__pycache__"))
    return repo


def snapshot(repo):
    return {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def test_fix_writes_patch_and_leaves_repo_alone(tmp_path):
    repo = copy_repo(tmp_path)
    before = snapshot(repo)
    out = tmp_path / "out.patch"
    res = runner.invoke(app, ["fix", str(repo), "--output", str(out)])
    assert res.exit_code == 0, res.output
    assert "ruff fixed 6 of 8 candidate findings in 2 files" in res.output
    assert "2 auto-fix findings left" in res.output
    patch = out.read_text()
    assert "+++ b/app/utils.py" in patch and "+++ b/app/shapes.py" in patch
    assert "auth/tokens.py" not in patch
    assert snapshot(repo) == before


def test_fix_apply_writes_repo(tmp_path):
    repo = copy_repo(tmp_path)
    res = runner.invoke(app, ["fix", str(repo), "--output", str(tmp_path / "p"), "--apply"])
    assert res.exit_code == 0, res.output
    assert "applied to 2 files" in res.output
    assert len(scan(repo)[0].findings) == 15 - 6


def test_fix_bad_config_exits_2(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.patchwarden]\nnope = 1\n")
    res = runner.invoke(app, ["fix", str(tmp_path), "--output", str(tmp_path / "p")])
    assert res.exit_code == 2
