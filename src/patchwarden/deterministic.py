"""The deterministic pass: ruff's own safe fixes, for allowlisted findings only, before any LLM.

Never repo-wide: `ruff check --fix` on a whole repo also fixes findings in protected paths and
tests. Instead ruff runs per file, selecting only that file's `auto_fix_allowed` rules. Each file
is then re-scanned and kept only if its selected findings went away, nothing new appeared and
check_diff is clean; otherwise it is reverted.
"""

import subprocess
import sys
from collections import defaultdict

from pydantic import BaseModel, Field

from patchwarden.analyzers import Analyzer, AnalyzerError, run_analyzers
from patchwarden.config import Config
from patchwarden.models import Finding, PreClassKind, ScanResult
from patchwarden.policy import check_diff
from patchwarden.workspace import Workspace


class DeterministicResult(BaseModel):
    resolved: list[str] = Field(default_factory=list)  # fingerprints fixed by ruff
    fixed_files: list[str] = Field(default_factory=list)
    reverted: dict[str, str] = Field(default_factory=dict)  # file -> why ruff's fix was dropped
    # Findings still present afterwards, with regions as they are now: ruff's fixes shift lines.
    remaining: list[Finding] = Field(default_factory=list)


def candidates(result: ScanResult) -> dict[str, list[Finding]]:
    """Ruff findings on the auto-fix allowlist, by file."""
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in result.findings:
        if f.tool == "ruff" and result.preclass[f.fingerprint].kind == (
            PreClassKind.auto_fix_allowed
        ):
            by_file[f.file].append(f)
    return dict(by_file)


def ruff_fix(ws: Workspace, file: str, codes: list[str], timeout: int = 120) -> None:
    # No --unsafe-fixes: those can change behaviour (F841 drops an assignment's side effects).
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--isolated",
        "--no-cache",
        "--fix-only",
        "--quiet",
        "--select",
        ",".join(codes),
        "--",
        file,
    ]
    try:
        proc = subprocess.run(cmd, cwd=ws.root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AnalyzerError(f"ruff --fix timed out after {timeout}s on {file}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-500:]
        raise AnalyzerError(f"ruff --fix exited {proc.returncode} on {file}: {tail}")


def _strip_new_leading_blank_lines(ws: Workspace, file: str) -> None:
    """Removing a file's leading imports leaves it starting with blank lines; drop them unless
    the file started that way."""
    before, after = ws.original(file), ws.read(file)
    if after != before and before[:1] not in ("\n", "\r") and after[:1] in ("\n", "\r"):
        ws.write(file, after.lstrip("\r\n"))


def run_deterministic(
    ws: Workspace,
    result: ScanResult,
    cfg: Config,
    analyzers: list[Analyzer] | None = None,
) -> DeterministicResult:
    out = DeterministicResult()
    by_file = candidates(result)
    for file, selected in sorted(by_file.items()):
        ruff_fix(ws, file, sorted({f.rule_id.removeprefix("ruff:") for f in selected}))
        _strip_new_leading_blank_lines(ws, file)
    changed = [f for f in ws.changed_files() if f in by_file]
    if not changed:
        out.remaining = list(result.findings)
        return out
    after, _, _ = run_analyzers(ws.root, changed, cfg, analyzers)
    after_fps: dict[str, set[str]] = defaultdict(set)
    for f in after:
        after_fps[f.file].add(f.fingerprint)
    before_fps: dict[str, set[str]] = defaultdict(set)
    for f in result.findings:
        before_fps[f.file].add(f.fingerprint)

    for file in changed:
        selected = by_file[file]
        new = after_fps[file] - before_fps[file]
        resolved = [f.fingerprint for f in selected if f.fingerprint not in after_fps[file]]
        violations = check_diff(
            {file: ws.changes()[file]},
            cfg,
            target_file=file,
            rule_ids={f.rule_id for f in selected},
            max_lines=None,  # one run fixes many findings; the per-fix size limit doesn't apply
        )
        if new:
            reason = f"ruff's fix introduced {len(new)} new finding(s)"
        elif violations:
            reason = "; ".join(f"{v.kind}: {v.detail}" for v in violations)
        elif not resolved:
            reason = "ruff changed the file but resolved none of the selected findings"
        else:
            out.resolved += resolved
            out.fixed_files.append(file)
            continue
        ws.revert(file)
        out.reverted[file] = reason
    kept = set(out.fixed_files)
    out.remaining = [f for f in after if f.file in kept] + [
        f for f in result.findings if f.file not in kept
    ]
    out.remaining.sort(key=lambda f: (f.file, f.region.start_line, f.rule_id))
    return out


def undo_deterministic(
    ws: Workspace, result: ScanResult, det: DeterministicResult, reason: str
) -> DeterministicResult:
    """Revert every file ruff fixed (the tests failed afterwards); its findings go to the LLM."""
    for file in det.fixed_files:
        ws.revert(file)
    return DeterministicResult(
        reverted={**det.reverted, **dict.fromkeys(det.fixed_files, reason)},
        remaining=sorted(result.findings, key=lambda f: (f.file, f.region.start_line, f.rule_id)),
    )
