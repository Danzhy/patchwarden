"""The `scan` pipeline: scope -> analyzers -> findings -> pre-classification. No LLM, no writes."""

from pathlib import Path

from patchwarden.analyzers import Analyzer, run_analyzers
from patchwarden.config import Config, load_config
from patchwarden.models import ScanResult
from patchwarden.policy import pre_classify
from patchwarden.scope import files_in_scope


def scan(
    repo: Path,
    base: str | None = None,
    cfg: Config | None = None,
    analyzers: list[Analyzer] | None = None,
) -> tuple[ScanResult, list[str]]:
    """(result, warnings)."""
    repo = repo.resolve()
    cfg = cfg or load_config(repo)
    files, mode, warning = files_in_scope(repo, cfg, base)
    findings, ran, skipped = run_analyzers(repo, files, cfg, analyzers)
    warnings = [warning] if warning else []
    warnings += [f"{name} skipped: {why}" for name, why in skipped.items()]
    result = ScanResult(
        repo=str(repo),
        scope_mode=mode,
        files_scanned=files,
        analyzers_run=ran,
        analyzers_skipped=skipped,
        findings=findings,
        preclass={f.fingerprint: pre_classify(f, cfg) for f in findings},
        config_hash=cfg.config_hash(),
    )
    return result, warnings
