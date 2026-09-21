"""CodeQL analyzer. Optional: skipped with a message when the CLI isn't installed.

Slow on first use (query compilation took ~7 min in M0; ~17 s after), so it's off by default
and mainly used by eval A.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from patchwarden.analyzers.base import AnalyzerError
from patchwarden.analyzers.sarif import parse_sarif
from patchwarden.models import Finding

DEFAULT_SUITE = "codeql/python-queries:codeql-suites/python-code-scanning.qls"


def find_codeql() -> str | None:
    env = os.environ.get("CODEQL")
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("codeql")
    if found:
        return found
    home = Path.home() / "codeql-home/codeql/codeql"
    return str(home) if home.is_file() else None


def _run(cmd: list[str], what: str, timeout: int) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AnalyzerError(f"codeql {what} timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise AnalyzerError(f"codeql {what} exited {proc.returncode}: {proc.stderr[-500:]}")


class CodeQLAnalyzer:
    name = "codeql"

    def __init__(self, queries: list[str] | None = None, timeout: int = 1800):
        self.binary = find_codeql()
        self.queries = queries or [os.environ.get("CODEQL_SUITE", DEFAULT_SUITE)]
        self.timeout = timeout

    def available(self) -> str | None:
        if self.binary is None:
            return "codeql binary not found (set $CODEQL or put codeql on PATH)"
        return None

    def run(self, repo: Path, files: list[str]) -> list[Finding]:
        if not files or self.binary is None:
            return []
        # CodeQL analyses a whole source tree; build the DB over the repo, then keep only
        # findings in the requested files.
        with tempfile.TemporaryDirectory(prefix="patchwarden-codeql-") as tmp:
            db, out = Path(tmp) / "db", Path(tmp) / "results.sarif"
            _run(
                [
                    self.binary,
                    "database",
                    "create",
                    str(db),
                    "--language=python",
                    f"--source-root={repo}",
                    "-q",
                ],
                "database create",
                self.timeout,
            )
            _run(
                [
                    self.binary,
                    "database",
                    "analyze",
                    str(db),
                    *self.queries,
                    "--format=sarif-latest",
                    f"--output={out}",
                    "--threads=0",
                    "-q",
                ],
                "database analyze",
                self.timeout,
            )
            try:
                doc = json.loads(out.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise AnalyzerError(f"codeql produced no readable SARIF: {e}") from e
        wanted = set(files)
        return [f for f in parse_sarif(doc, self.name, repo) if f.file in wanted]
