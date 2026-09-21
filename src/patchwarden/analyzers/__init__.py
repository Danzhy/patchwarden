"""Static analyzers that emit SARIF, normalised to Finding."""

from pathlib import Path

from patchwarden.analyzers.bandit import BanditAnalyzer
from patchwarden.analyzers.base import Analyzer, AnalyzerError
from patchwarden.analyzers.codeql import CodeQLAnalyzer
from patchwarden.analyzers.ruff import RuffAnalyzer
from patchwarden.config import Config, ConfigError
from patchwarden.models import Finding

__all__ = ["Analyzer", "AnalyzerError", "build_analyzers", "run_analyzers"]


def build_analyzers(cfg: Config) -> list[Analyzer]:
    known = {
        "ruff": lambda: RuffAnalyzer(cfg.ruff_select),
        "bandit": lambda: BanditAnalyzer(skip_paths=cfg.test_paths),
        "codeql": lambda: CodeQLAnalyzer(cfg.codeql_queries or None),
    }
    unknown = [a for a in cfg.analyzers if a not in known]
    if unknown:
        raise ConfigError(f"unknown analyzers: {', '.join(unknown)} (known: {', '.join(known)})")
    return [known[a]() for a in cfg.analyzers]


def run_analyzers(
    repo: Path, files: list[str], cfg: Config, analyzers: list[Analyzer] | None = None
) -> tuple[list[Finding], list[str], dict[str, str]]:
    """(findings, analyzers run, analyzers skipped -> reason)."""
    findings: list[Finding] = []
    ran: list[str] = []
    skipped: dict[str, str] = {}
    for a in analyzers if analyzers is not None else build_analyzers(cfg):
        reason = a.available()
        if reason:
            skipped[a.name] = reason
            continue
        findings.extend(a.run(repo, files))
        ran.append(a.name)
    findings.sort(key=lambda f: (f.file, f.region.start_line, f.rule_id))
    return findings, ran, skipped
