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
    apply: Annotated[
        bool, typer.Option("--apply", help="Also write the fixes to the repo.")
    ] = False,
) -> None:
    """Fix findings in a temporary copy and write a patch. The repo changes only with --apply.

    For now this is the deterministic pass only (ruff's safe fixes for allowlisted rules);
    the LLM stages come in M3.
    """
    from patchwarden.analyzers import AnalyzerError
    from patchwarden.config import ConfigError, load_config
    from patchwarden.deterministic import candidates, run_deterministic
    from patchwarden.models import PreClassKind
    from patchwarden.scan import scan as run_scan
    from patchwarden.workspace import WorkspaceError, open_workspace

    try:
        cfg = load_config(repo.resolve())
        result, warnings = run_scan(repo, base=base, cfg=cfg)
        for w in warnings:
            typer.echo(f"warning: {w}", err=True)
        with open_workspace(repo) as ws:
            det = run_deterministic(ws, result, cfg)
            patch = ws.diff()
            applied = ws.apply_to_source() if apply and patch else []
    except (ConfigError, AnalyzerError, WorkspaceError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e

    for file, why in det.reverted.items():
        typer.echo(f"reverted ruff's fix in {file}: {why}", err=True)
    output.write_text(patch)
    allowed = sum(1 for pc in result.preclass.values() if pc.kind == PreClassKind.auto_fix_allowed)
    by_ruff = sum(len(v) for v in candidates(result).values())
    typer.echo(
        f"ruff fixed {len(det.resolved)} of {by_ruff} candidate findings in "
        f"{len(det.fixed_files)} files; {allowed - len(det.resolved)} auto-fix findings "
        "left for the Fixer (M3)."
    )
    typer.echo(f"patch: {output}" + ("" if patch else " (empty)"))
    if applied:
        typer.echo(f"applied to {len(applied)} files: {', '.join(applied)}")
