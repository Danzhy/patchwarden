import subprocess

import pytest

from patchwarden.workspace import WorkspaceError, open_workspace


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg/a.py").write_text("one\ntwo\n")
    (repo / "b.py").write_text("no newline")
    (repo / "crlf.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    (repo / ".venv/lib").mkdir(parents=True)
    (repo / ".venv/lib/big.py").write_text("x = 1\n")
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("hi\n")
    return repo


def test_copy_skips_venv_and_git_and_is_removed(tmp_path):
    repo = make_repo(tmp_path)
    with open_workspace(repo) as ws:
        root = ws.root
        assert (root / "pkg/a.py").is_file() and (root / "README.md").is_file()
        assert not (root / ".venv").exists() and not (root / ".git").exists()
        assert ws.changes() == {} and ws.diff() == ""
    assert not root.exists()


def test_writes_stay_in_workspace(tmp_path):
    repo = make_repo(tmp_path)
    with open_workspace(repo) as ws:
        ws.write("pkg/a.py", "one\n")
        assert ws.changed_files() == ["pkg/a.py"]
        ws.revert("pkg/a.py")
        assert ws.changes() == {}
        ws.write("pkg/a.py", "one\n")
    assert (repo / "pkg/a.py").read_text() == "one\ntwo\n"


def test_only_python_files_are_writable(tmp_path):
    with open_workspace(make_repo(tmp_path)) as ws:
        for rel in ["README.md", "new.py", "../x.py", "/abs.py"]:
            with pytest.raises(WorkspaceError):
                ws.write(rel, "x")


def test_diff_applies_with_git(tmp_path):
    repo = make_repo(tmp_path)
    with open_workspace(repo) as ws:
        ws.write("pkg/a.py", "one\n2\n")
        ws.write("b.py", "still no newline")
        ws.write("crlf.py", "x = 1\r\ny = 3\r\n")
        patch = ws.diff()
    assert "\\ No newline at end of file" in patch
    (tmp_path / "p.patch").write_text(patch, newline="")
    subprocess.run(
        ["git", "apply", "--check", str(tmp_path / "p.patch")],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_apply_to_source(tmp_path):
    repo = make_repo(tmp_path)
    with open_workspace(repo) as ws:
        ws.write("pkg/a.py", "one\n")
        ws.write("crlf.py", "x = 1\r\n")
        assert sorted(ws.apply_to_source()) == ["crlf.py", "pkg/a.py"]
    assert (repo / "pkg/a.py").read_text() == "one\n"
    assert (repo / "crlf.py").read_bytes() == b"x = 1\r\n"


def test_apply_refuses_when_source_changed(tmp_path):
    repo = make_repo(tmp_path)
    with open_workspace(repo) as ws:
        ws.write("pkg/a.py", "one\n")
        ws.write("b.py", "changed")
        (repo / "pkg/a.py").write_text("edited by the user\n")
        with pytest.raises(WorkspaceError, match="pkg/a.py"):
            ws.apply_to_source()
    assert (repo / "b.py").read_text() == "no newline"  # nothing written


def test_non_utf8_file_is_left_alone(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "latin.py").write_bytes(b"s = '\xe9'\n")
    with open_workspace(repo) as ws:
        assert not ws.exists("latin.py")
