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
