"""patchwarden command line: scan, fix, init-ci, trace, eval."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Fix static-analysis warnings safely.", no_args_is_help=True)


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


@app.command()
def version() -> None:
    """Print the patchwarden version."""
    from importlib.metadata import version as pkg_version

    typer.echo(pkg_version("patchwarden"))


@app.command()
def scan(
    repo: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Repository to scan.")
    ] = Path("."),
    base: Annotated[
        str | None, typer.Option(help="Only scan .py files changed in BASE...HEAD.")
    ] = None,
    fmt: Annotated[OutputFormat, typer.Option("--format", help="Report format.")] = (
        OutputFormat.text
    ),
    output: Annotated[
        Path | None, typer.Option(help="Write the report here instead of stdout.")
    ] = None,
) -> None:
    """Analyze and pre-classify findings; print the report. Never modifies the repo."""
    from patchwarden.agents.reporter import render_scan_text
    from patchwarden.analyzers import AnalyzerError
    from patchwarden.config import ConfigError
    from patchwarden.scan import scan as run_scan

    try:
        result, warnings = run_scan(repo, base=base)
    except (ConfigError, AnalyzerError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    for w in warnings:
        typer.echo(f"warning: {w}", err=True)
    text = (
        result.model_dump_json(indent=2) + "\n"
        if fmt == OutputFormat.json
        else render_scan_text(result)
    )
    if output:
        output.write_text(text)
    else:
        typer.echo(text, nl=False)


@app.command()
def fix(
    repo: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Repository to fix.")
    ] = Path("."),
    base: Annotated[
        str | None, typer.Option(help="Only fix .py files changed in BASE...HEAD.")
    ] = None,
    output: Annotated[Path, typer.Option(help="Where to write the unified diff.")] = Path(
        "patchwarden.patch"
    ),
    report: Annotated[Path, typer.Option(help="Where to write the markdown report.")] = Path(
        "patchwarden-report.md"
    ),
    trace_dir: Annotated[
        Path, typer.Option(help="Trace store directory (traces.db + runs/*.jsonl).")
    ] = Path(".patchwarden"),
    apply: Annotated[
        bool, typer.Option("--apply", help="Also write the auto-fixes to the repo.")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Only ruff's safe fixes; no LLM calls.")
    ] = False,
    budget_usd: Annotated[
        float | None, typer.Option(help="Stop calling the LLM after this much spend (USD).")
    ] = None,
) -> None:
    """Fix findings in a temporary copy; write a patch and a report. The repo changes only
    with --apply. Exit code 1 if anything needs a human (escalated or not fixable)."""
    import dataclasses

    from patchwarden.analyzers import AnalyzerError
    from patchwarden.config import ConfigError, load_config
    from patchwarden.llm import LLMError
    from patchwarden.models import FindingStatus
    from patchwarden.pipeline import run_fix
    from patchwarden.tracing.store import TraceStore
    from patchwarden.workspace import WorkspaceError

    try:
        cfg = load_config(repo.resolve())
        if budget_usd is not None:
            cfg = dataclasses.replace(cfg, budget_usd=budget_usd)
        store = TraceStore(trace_dir)
        try:
            res = run_fix(
                repo, cfg, store=store, llm_factory=make_llm, base=base, no_llm=no_llm, apply=apply
            )
        finally:
            store.close()
    except (ConfigError, AnalyzerError, WorkspaceError, LLMError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e

    for w in res.warnings:
        typer.echo(f"warning: {w}", err=True)
    for file, why in res.reverted.items():
        typer.echo(f"reverted ruff's fix in {file}: {why}", err=True)
    for path in (output, report):
        path.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(res.patch)
    report.write_text(res.report(str(output)))

    n = {s: sum(1 for o in res.outcomes if o.status == s) for s in FindingStatus}
    by_ruff = sum(1 for o in res.outcomes if o.fixed_by == "ruff")
    typer.echo(
        f"ruff fixed {by_ruff} of {res.ruff_candidates} candidate findings; "
        f"Fixer fixed {n[FindingStatus.fixed] - by_ruff}; suggested {n[FindingStatus.suggested]}; "
        f"escalated {n[FindingStatus.escalated] + n[FindingStatus.failed]}; "
        f"false positive {n[FindingStatus.false_positive]}"
        + (f"; not triaged {n[FindingStatus.not_triaged]}" if no_llm else "")
        + "."
    )
    typer.echo(f"patch: {output}" + ("" if res.patch else " (empty)"))
    typer.echo(f"report: {report}")
    typer.echo(f"trace: {res.run_id} in {trace_dir} (cost ${res.cost_usd:.4f}, {res.outcome})")
    if res.applied:
        typer.echo(f"applied to {len(res.applied)} files: {', '.join(res.applied)}")
    if res.apply_error:
        typer.echo(f"error: --apply refused, nothing written: {res.apply_error}", err=True)
        raise typer.Exit(2)
    if res.needs_human:
        raise typer.Exit(1)


def make_llm(cfg, run):
    """The LLM client for `fix`; tests replace this with a fake transport."""
    from patchwarden.llm import make_client

    return make_client(cfg, run)
