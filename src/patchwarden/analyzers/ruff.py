"""Ruff analyzer. Runs the ruff installed alongside patchwarden, with patchwarden's rule set."""

import sys
from pathlib import Path

from patchwarden.analyzers.base import run_sarif_tool
from patchwarden.analyzers.sarif import parse_sarif
from patchwarden.models import Finding


class RuffAnalyzer:
    name = "ruff"

    def __init__(self, select: list[str]):
        self.select = select

    def available(self) -> str | None:
        return None  # a dependency of patchwarden

    def run(self, repo: Path, files: list[str]) -> list[Finding]:
        if not files:
            return []
        # --isolated: the repo's own ruff config doesn't change which rules we report, so
        # results (and evals) depend only on patchwarden's config.
        cmd = [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            ",".join(self.select),
            "--output-format",
            "sarif",
            "--",
            *files,
        ]
        return parse_sarif(run_sarif_tool(self.name, cmd, repo), self.name, repo)
