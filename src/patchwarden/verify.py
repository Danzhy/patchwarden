"""The code checks on one fix, before any LLM judges it:

- the edited file parses (ast.parse);
- a re-scan of the file with every configured analyzer: the target finding is gone, nothing new
  appeared, and no *other* finding disappeared (a fix changes one finding's code, nothing else);
- the repo's tests pass (test_command), run in the workspace with secrets removed from the
  environment.

Findings are matched before/after by fingerprint, and the line has to agree too: fingerprints
of identical snippets carry an occurrence suffix (":1"), which shifts when an earlier duplicate
is fixed. An edit that touches a flagged line changes that finding's snippet, and so its
fingerprint, without fixing it; so what's left is paired by rule and the nearest line within
the fix size limit (a fix can't move a finding further than it changes lines).
"""

import ast
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from patchwarden.analyzers import Analyzer, AnalyzerError, run_analyzers
from patchwarden.config import Config
from patchwarden.models import Finding, TestStatus, VerifyResult
from patchwarden.workspace import Workspace, line_mapper

# Test commands run repo code (after an LLM edited it): they get no credentials.
_SECRET_NAME = re.compile(r"API_KEY|TOKEN|SECRET|PASSWORD|OPENROUTER", re.IGNORECASE)
OUTPUT_TAIL = 800


def scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}


def run_tests(root: Path, cmd: str, timeout: int) -> tuple[TestStatus, str]:
    """Run `cmd` (split like a shell would, but no shell) in `root`. (status, detail)."""
    env = scrubbed_env()
    try:
        argv = shlex.split(cmd)
        # Resolved up front: a directory named like the command on PATH (CodeQL ships one
        # called `python`) otherwise fails as a baffling "Permission denied".
        exe = argv[0] if argv and "/" in argv[0] else None  # a path: relative to root
        if argv and exe is None:
            exe = shutil.which(argv[0], path=env.get("PATH"))
        if exe is None:
            return TestStatus.error, f"`{cmd}` could not run: {argv[:1]} not found on PATH"
        proc = subprocess.run(
            [exe, *argv[1:]],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return TestStatus.timeout, f"`{cmd}` gave no result after {timeout}s"
    except (OSError, ValueError) as e:
        return TestStatus.error, f"`{cmd}` could not run: {e}"
    tail = (proc.stdout + proc.stderr).strip()[-OUTPUT_TAIL:]
    if proc.returncode == 0:
        return TestStatus.passed, tail.splitlines()[-1] if tail else ""
    return TestStatus.failed, f"`{cmd}` exited {proc.returncode}: {tail}"


class TestRunner:
    """cfg.test_command in the workspace. Call `baseline()` once before any change: tests that
    already fail can't tell a good fix from a bad one, so then every fix counts as untested."""

    __test__ = False  # not a pytest test class

    def __init__(self, cfg: Config, root: Path):
        self.cmd = cfg.test_command
        self.timeout = cfg.test_timeout_s
        self.root = root
        self.skip_reason = None if self.cmd else "no test_command configured"

    def baseline(self) -> tuple[TestStatus, str]:
        if not self.cmd:
            return TestStatus.skipped, self.skip_reason or ""
        status, detail = run_tests(self.root, self.cmd, self.timeout)
        if status != TestStatus.passed:
            self.skip_reason = f"the tests already {status} before any change ({detail[-300:]})"
        return status, detail

    def run(self) -> tuple[TestStatus, str]:
        if self.skip_reason or not self.cmd:
            return TestStatus.skipped, self.skip_reason or ""
        return run_tests(self.root, self.cmd, self.timeout)


def describe(f: Finding) -> str:
    return f"{f.rule_id} at {f.location}: {f.message}"


def _base(fp: str) -> str:
    return fp.split(":", 1)[0]


def match_findings(
    before: list[Finding],
    after: list[Finding],
    new_line: Callable[[int], int],
    radius: int,
) -> tuple[list[int], list[int]]:
    """(indexes into `before` with no match after, indexes into `after` with none before)."""
    free = set(range(len(after)))

    def take(i: int, same: Callable[[Finding, Finding, int], bool]) -> bool:
        want = new_line(before[i].region.start_line)
        cands = [j for j in free if same(before[i], after[j], want)]
        if cands:
            free.discard(min(cands, key=lambda j: (abs(after[j].region.start_line - want), j)))
        return bool(cands)

    def same_fp(b: Finding, a: Finding, want: int) -> bool:
        return _base(a.fingerprint) == _base(b.fingerprint) and a.region.start_line == want

    def same_rule_near(b: Finding, a: Finding, want: int) -> bool:
        return a.rule_id == b.rule_id and abs(a.region.start_line - want) <= radius

    left = [i for i in range(len(before)) if not take(i, same_fp)]
    left = [i for i in left if not take(i, same_rule_near)]
    return left, sorted(free)


def locate(target: Finding, findings: list[Finding], radius: int) -> int | None:
    """The index of `target` in a list of current findings: by fingerprint, else the same rule
    on the same (or the nearest) line."""
    for i, f in enumerate(findings):
        if f.fingerprint == target.fingerprint:
            return i
    near = [
        i
        for i, f in enumerate(findings)
        if f.rule_id == target.rule_id
        and abs(f.region.start_line - target.region.start_line) <= radius
    ]
    if not near:
        return None
    return min(near, key=lambda i: abs(findings[i].region.start_line - target.region.start_line))


def verify_fix(
    ws: Workspace,
    target: Finding,
    before_text: str,
    before: list[Finding],
    cfg: Config,
    tests: TestRunner,
    analyzers: list[Analyzer] | None = None,
) -> tuple[VerifyResult, list[Finding] | None]:
    """Check the fix now in the workspace for `target`. `before`: the file's findings before
    the fix. Returns the result and the file's findings after it (None if it wasn't re-scanned);
    the tests run only when everything else passed, since they're the slow part."""
    after_text = ws.read(target.file)
    try:
        ast.parse(after_text)
    except SyntaxError as e:
        return VerifyResult(syntax_ok=False, error=f"line {e.lineno}: {e.msg}"), None
    try:
        after, _, _ = run_analyzers(ws.root, [target.file], cfg, analyzers)
    except AnalyzerError as e:
        return VerifyResult(syntax_ok=True, error=str(e)), None
    before = list(before)
    t = locate(target, before, cfg.max_lines_changed)
    if t is None:  # not expected (the pipeline keeps `before` current), but never lose it
        before.append(target)
        t = len(before) - 1
    gone, new = match_findings(
        before, after, line_mapper(before_text, after_text), cfg.max_lines_changed
    )
    res = VerifyResult(
        syntax_ok=True,
        target_gone=t in gone,
        new_findings=[describe(after[j]) for j in new],
        also_resolved=[describe(before[i]) for i in gone if i != t],
    )
    if res.passed:
        res.tests, res.tests_detail = tests.run()
    return res, after
