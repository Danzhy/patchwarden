"""`patchwarden init-ci`: render the workflow template for a repo."""

import re
from importlib import resources
from pathlib import Path

from patchwarden.scope import _git

WORKFLOW_PATH = Path(".github/workflows/patchwarden.yml")
DEFAULT_INSTALL = "patchwarden"
_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")


class InitCIError(ValueError):
    pass


def default_branch(repo: Path) -> str:
    """origin's default branch if git knows it (a clone), else "main"."""
    out = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return out.strip().removeprefix("origin/") if out and out.strip() else "main"


def not_repo_root(repo: Path) -> str | None:
    """Why `repo` is the wrong place for the workflow, or None. GitHub only reads
    .github/workflows at the top of the repository, and the workflow runs `fix .` there."""
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top is None:
        return None  # not a git repo (yet): nothing to compare against
    if Path(top.strip()).resolve() != repo.resolve():
        return f"{repo} is not the top of its git repository ({top.strip()})"
    return None


def render_workflow(install: str, branch: str) -> str:
    """The template with the pip install spec and the default branch filled in. Both land in
    YAML (single-quoted) and in shell via env, so they are checked, not escaped."""
    if not install.strip() or any(c in install for c in "'\"\n\r`$\\"):
        raise InitCIError(f"--install: not a plain pip requirement: {install!r}")
    if not _BRANCH.fullmatch(branch) or branch.startswith("-"):
        raise InitCIError(f"--default-branch: not a branch name: {branch!r}")
    text = resources.files("patchwarden.ci").joinpath("workflow_template.yml").read_text()
    return text.replace("@@INSTALL@@", install.strip()).replace("@@DEFAULT_BRANCH@@", branch)
