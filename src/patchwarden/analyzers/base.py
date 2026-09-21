"""Analyzer protocol and the shared subprocess runner."""

import json
import subprocess
from pathlib import Path
from typing import Protocol

from patchwarden.models import Finding


class AnalyzerError(RuntimeError):
    pass


class Analyzer(Protocol):
    name: str

    def available(self) -> str | None:
        """None if the analyzer can run, else the reason it can't (shown as "skipped")."""

    def run(self, repo: Path, files: list[str]) -> list[Finding]:
        """Findings for `files` (repo-relative) only."""


def run_sarif_tool(name: str, cmd: list[str], cwd: Path, timeout: int = 600) -> dict:
    """Run a tool that prints SARIF to stdout. Exit 0 = clean, 1 = findings; anything else fails."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AnalyzerError(f"{name} timed out after {timeout}s") from e
    if proc.returncode not in (0, 1):
        tail = (proc.stderr or proc.stdout).strip()[-500:]
        raise AnalyzerError(f"{name} exited {proc.returncode}: {tail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AnalyzerError(f"{name} printed invalid SARIF: {e}") from e
