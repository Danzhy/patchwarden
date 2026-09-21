"""patchwarden command line: scan, fix, init-ci, trace, eval."""

import typer

app = typer.Typer(help="Fix static-analysis warnings safely.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the patchwarden version."""
    from importlib.metadata import version as pkg_version

    typer.echo(pkg_version("patchwarden"))


@app.command()
def scan() -> None:
    """Analyze and triage only; print the report. (M1)"""
    typer.echo("scan: not implemented yet (M1)")
    raise typer.Exit(2)
