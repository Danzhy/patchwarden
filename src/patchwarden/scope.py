"""Which files to scan: changed .py files against a base ref, or the whole repo."""

import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from patchwarden.config import Config

SKIP_DIRS = {".git", ".venv", "venv", ".tox", ".nox", "node_modules", "__pycache__", ".patchwarden"}


def match_path(path: str, patterns: list[str]) -> str | None:
    """The first pattern matching a repo-relative POSIX path, else None.

    fnmatch's `*` crosses `/`, so `**/auth/**` means "an auth directory at any depth". Matching
    against "/" + path too lets it also match a top-level `auth/x.py`.
    """
    for pat in patterns:
        if fnmatchcase(path, pat) or fnmatchcase("/" + path, pat):
            return pat
    return None


def is_test_path(path: str, cfg: Config) -> bool:
    return match_path(path, cfg.test_paths) is not None


def all_python_files(repo: Path, cfg: Config) -> list[str]:
    out = []
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        rel_s = rel.as_posix()
        if match_path(rel_s, cfg.exclude) is None:
            out.append(rel_s)
    return sorted(out)


def changed_python_files(repo: Path, base: str, cfg: Config) -> list[str] | None:
    """Added/modified .py files in `base...HEAD`, or None if that can't be computed."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD", "--", "*.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    # git prints paths relative to the repo top level; make them relative to `repo`.
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    out = []
    for line in proc.stdout.splitlines():
        p = (Path(top) / line).resolve()
        try:
            rel = p.relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue
        if p.is_file() and match_path(rel, cfg.exclude) is None:
            out.append(rel)
    return sorted(out)


def files_in_scope(repo: Path, cfg: Config, base: str | None) -> tuple[list[str], str, str | None]:
    """(files, mode, warning). mode is "diff" or "full"; warning explains a fallback."""
    if base:
        changed = changed_python_files(repo, base, cfg)
        if changed is not None:
            return changed, "diff", None
        warning = f"cannot diff against {base!r} (not a git repo or unknown ref); scanning all"
        return all_python_files(repo, cfg), "full", warning
    return all_python_files(repo, cfg), "full", None
