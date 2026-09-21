"""A throwaway copy of the repo that every fix is made in. Only `apply_to_source` (--apply)
writes to the user's tree.

A plain copy rather than a git worktree: a worktree starts from a commit, so it would silently
drop the user's uncommitted changes.
"""

import contextlib
import difflib
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from patchwarden.scope import SKIP_DIRS


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, source: Path, root: Path):
        self.source = source
        self.root = root
        shutil.copytree(source, root, ignore=shutil.ignore_patterns(*SKIP_DIRS), symlinks=True)
        # Only regular UTF-8 .py files are editable; anything else is never touched.
        self._snapshot: dict[str, str] = {}
        for p in sorted(root.rglob("*.py")):
            if p.is_file() and not p.is_symlink():
                with contextlib.suppress(UnicodeDecodeError):
                    self._snapshot[p.relative_to(root).as_posix()] = read_text(p)

    def _path(self, rel: str) -> Path:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise WorkspaceError(f"path outside the workspace: {rel}")
        return self.root / pure

    def exists(self, rel: str) -> bool:
        return rel in self._snapshot and self._path(rel).is_file()

    def read(self, rel: str) -> str:
        return read_text(self._path(rel))

    def write(self, rel: str, text: str) -> None:
        if rel not in self._snapshot:
            # Fixes edit existing Python files; creating files isn't a fix.
            raise WorkspaceError(f"not a Python file in the repo: {rel}")
        write_text(self._path(rel), text)

    def original(self, rel: str) -> str:
        return self._snapshot[rel]

    def revert(self, rel: str) -> None:
        write_text(self._path(rel), self._snapshot[rel])

    def changes(self) -> dict[str, tuple[str, str]]:
        """{file: (before, after)} for every Python file that differs from the snapshot.

        Compared on disk, so edits made by subprocesses (ruff --fix) are included.
        """
        out = {}
        for rel, before in self._snapshot.items():
            path = self._path(rel)
            after = read_text(path) if path.is_file() else ""
            if after != before:
                out[rel] = (before, after)
        return out

    def changed_files(self) -> list[str]:
        return list(self.changes())

    def diff(self) -> str:
        """Unified diff of all changes, in the a/ b/ form `git apply` and `patch -p1` take."""
        return "".join(unified_diff(rel, b, a) for rel, (b, a) in self.changes().items())

    def apply_to_source(self) -> list[str]:
        """Write the changed files back to the user's repo. Refuses (writing nothing) if any
        of them changed in the source since the workspace was created."""
        changes = self.changes()
        stale = [rel for rel in changes if _read(self.source / rel) != self._snapshot[rel]]
        if stale:
            raise WorkspaceError(f"changed since scan, not applied: {', '.join(stale)}")
        for rel, (_, after) in changes.items():
            write_text(self.source / rel, after)
        return list(changes)


def read_text(path: Path) -> str:
    """Exact contents; newline="" keeps CRLF files CRLF."""
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path: Path) -> str | None:
    return read_text(path) if path.is_file() else None


def split_lines(text: str) -> list[str]:
    """Lines with their endings, split on "\n" only (str.splitlines also splits on form
    feeds and other characters that aren't line breaks to git)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def unified_diff(rel: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        split_lines(before),
        split_lines(after),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    out = []
    for line in lines:
        if not line.endswith("\n"):
            # Last line without a trailing newline: git's marker, else the patch is corrupt.
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    return "".join(out)


@contextmanager
def open_workspace(repo: Path) -> Iterator[Workspace]:
    tmp = Path(tempfile.mkdtemp(prefix="patchwarden-"))
    try:
        yield Workspace(repo.resolve(), tmp / "repo")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
