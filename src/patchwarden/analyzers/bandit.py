"""Bandit analyzer (security). Test files are skipped: B101 fires on every `assert`."""

import sys
from pathlib import Path

from patchwarden.analyzers.base import run_sarif_tool
from patchwarden.analyzers.sarif import parse_sarif
from patchwarden.models import Finding
from patchwarden.scope import match_path


class BanditAnalyzer:
    name = "bandit"

    def __init__(self, skip_paths: list[str] | None = None):
        self.skip_paths = skip_paths or []

    def available(self) -> str | None:
        return None  # a dependency of patchwarden

    def run(self, repo: Path, files: list[str]) -> list[Finding]:
        files = [f for f in files if match_path(f, self.skip_paths) is None]
        if not files:
            return []
        cmd = [sys.executable, "-m", "bandit", "-q", "-f", "sarif", "--", *files]
        return parse_sarif(run_sarif_tool(self.name, cmd, repo), self.name, repo)
