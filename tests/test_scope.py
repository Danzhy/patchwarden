import subprocess

import pytest

from patchwarden.config import Config, from_dict
from patchwarden.scope import files_in_scope, match_path


def write(root, rel, text="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("app/auth/tokens.py", ["**/auth/**"], "**/auth/**"),
        ("auth/tokens.py", ["**/auth/**"], "**/auth/**"),  # top-level dir
        ("app/oauth/x.py", ["**/auth/**"], None),  # not a substring match
        ("pkg/settings_prod.py", ["**/settings*.py"], "**/settings*.py"),
        ("tests/test_a.py", ["tests/**"], "tests/**"),
        ("app/x.py", [], None),
    ],
)
def test_match_path(path, patterns, expected):
    assert match_path(path, patterns) == expected


def test_full_scope_skips_venv_and_excludes(tmp_path):
    write(tmp_path, "a.py")
    write(tmp_path, "pkg/b.py")
    write(tmp_path, ".venv/lib/c.py")
    write(tmp_path, "build/d.py")
    write(tmp_path, "notes.txt")
    cfg = from_dict({"exclude": ["build/**"]})
    files, mode, warning = files_in_scope(tmp_path, cfg, None)
    assert files == ["a.py", "pkg/b.py"]
    assert (mode, warning) == ("full", None)


def test_base_outside_git_falls_back_with_warning(tmp_path):
    write(tmp_path, "a.py")
    files, mode, warning = files_in_scope(tmp_path, Config(), "main")
    assert files == ["a.py"] and mode == "full"
    assert "main" in warning


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_diff_scope_only_changed_python_files(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "old.py")
    write(tmp_path, "gone.py")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "feature")
    write(tmp_path, "old.py", "x = 2\n")
    write(tmp_path, "pkg/new.py")
    write(tmp_path, "README.md")
    (tmp_path / "gone.py").unlink()
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "change")
    files, mode, warning = files_in_scope(tmp_path, Config(), "main")
    assert files == ["old.py", "pkg/new.py"]
    assert (mode, warning) == ("diff", None)
