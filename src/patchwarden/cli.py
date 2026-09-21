"""patchwarden command line: scan, fix, init-ci, trace, eval."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Fix static-analysis warnings safely.", no_args_is_help=True)
trace_app = typer.Typer(help="Inspect runs in the trace store.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")

TraceDir = Annotated[Path, typer.Option(help="Trace store directory (traces.db + runs/*.jsonl).")]
ConfigFrom = Annotated[
    str | None,
    typer.Option(
        help="Read [tool.patchwarden] from pyproject.toml at this git ref (CI: the base branch), "
        "not from the working tree."
    ),
]


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
    config_from: ConfigFrom = None,
) -> None:
    """Analyze and pre-classify findings; print the report. Never modifies the repo."""
    from patchwarden.agents.reporter import render_scan_text
    from patchwarden.analyzers import AnalyzerError
    from patchwarden.config import ConfigError
    from patchwarden.scan import scan as run_scan

    try:
        cfg, cfg_warning = _config(repo, config_from)
        result, warnings = run_scan(repo, base=base, cfg=cfg)
    except (ConfigError, AnalyzerError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    for w in [cfg_warning] if cfg_warning else []:
        typer.echo(f"warning: {w}", err=True)
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
    trace_dir: TraceDir = Path(".patchwarden"),
    apply: Annotated[
        bool, typer.Option("--apply", help="Also write the auto-fixes to the repo.")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Only ruff's safe fixes; no LLM calls.")
    ] = False,
    budget_usd: Annotated[
        float | None, typer.Option(help="Stop calling the LLM after this much spend (USD).")
    ] = None,
    config_from: ConfigFrom = None,
) -> None:
    """Fix findings in a temporary copy; write a patch and a report. The repo changes only
    with --apply. Exit code 1 if anything needs a human (escalated or not fixable)."""
    import dataclasses
    from collections import Counter

    from patchwarden.analyzers import AnalyzerError
    from patchwarden.config import ConfigError
    from patchwarden.llm import LLMError
    from patchwarden.models import FindingStatus
    from patchwarden.pipeline import run_fix
    from patchwarden.tracing.store import TraceStore
    from patchwarden.workspace import WorkspaceError

    try:
        cfg, cfg_warning = _config(repo, config_from)
        if cfg_warning:
            typer.echo(f"warning: {cfg_warning}", err=True)
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
    if res.flags:
        counts = Counter(f.detector for f in res.flags)
        typer.echo(
            "flags: "
            + ", ".join(f"{d} x{n}" for d, n in counts.most_common())
            + f" (patchwarden trace show {res.run_id})"
        )
    if res.applied:
        typer.echo(f"applied to {len(res.applied)} files: {', '.join(res.applied)}")
    if res.apply_error:
        typer.echo(f"error: --apply refused, nothing written: {res.apply_error}", err=True)
        raise typer.Exit(2)
    if res.needs_human:
        raise typer.Exit(1)


def _config(repo: Path, config_from: str | None):
    """(config, warning): from the working tree, or from git ref `config_from`."""
    from patchwarden.config import load_config, load_config_at

    if config_from:
        return load_config_at(repo.resolve(), config_from)
    return load_config(repo.resolve()), None


@app.command("init-ci")
def init_ci(
    repo: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Repository to set up.")
    ] = Path("."),
    install: Annotated[
        str,
        typer.Option(
            help="What CI pip-installs, e.g. 'patchwarden==0.1.0' or "
            "'patchwarden @ git+https://github.com/OWNER/patchwarden@TAG'."
        ),
    ] = "patchwarden",
    default_branch: Annotated[
        str | None,
        typer.Option(help="Branch whose pushes are skipped. Default: origin's, else main."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing workflow.")] = False,
) -> None:
    """Write .github/workflows/patchwarden.yml: fix every PR, one PR comment, a trace artifact."""
    from patchwarden.ci.workflow import WORKFLOW_PATH, InitCIError, not_repo_root, render_workflow
    from patchwarden.ci.workflow import default_branch as detect_branch

    wrong_place = not_repo_root(repo)
    if wrong_place:
        typer.echo(f"error: {wrong_place}; run init-ci at the top", err=True)
        raise typer.Exit(2)
    target = repo / WORKFLOW_PATH
    if target.exists() and not force:
        typer.echo(f"error: {target} exists (use --force to overwrite)", err=True)
        raise typer.Exit(2)
    try:
        text = render_workflow(install, default_branch or detect_branch(repo))
    except InitCIError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    typer.echo(f"wrote {target}")
    typer.echo(
        "next:\n"
        "  1. add the repo secret OPENROUTER_API_KEY "
        "(gh secret set OPENROUTER_API_KEY)\n"
        '  2. set test_command under [tool.patchwarden] in pyproject.toml, e.g. "pytest -q";\n'
        "     without it, LLM fixes are only suggested\n"
        "  3. commit the workflow on the default branch: PRs read the policy from there"
    )


@app.command()
def comment(
    report: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="The markdown report from fix.")
    ],
    pr: Annotated[
        int | None, typer.Option(help="PR number. Default: from GITHUB_EVENT_PATH.")
    ] = None,
    repo_slug: Annotated[
        str | None, typer.Option("--repo", help="owner/name. Default: GITHUB_REPOSITORY.")
    ] = None,
) -> None:
    """Post the report as the PR's patchwarden comment, or update it (CI; needs GITHUB_TOKEN)."""
    import json
    import os

    from patchwarden.ci.comment import CommentError, GitHub, render_body, upsert_comment

    env = os.environ
    repo_slug = repo_slug or env.get("GITHUB_REPOSITORY")
    if pr is None and env.get("GITHUB_EVENT_PATH"):
        try:
            event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
            pr = (event.get("pull_request") or {}).get("number")
        except (OSError, ValueError):
            pr = None
    token = env.get("GITHUB_TOKEN")
    missing = [
        name
        for name, value in (("GITHUB_TOKEN", token), ("--repo", repo_slug), ("--pr", pr))
        if not value
    ]
    if missing:
        typer.echo(f"error: comment needs {', '.join(missing)}", err=True)
        raise typer.Exit(2)
    run_url = None
    if env.get("GITHUB_RUN_ID"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com")
        run_url = f"{server}/{repo_slug}/actions/runs/{env['GITHUB_RUN_ID']}"
    gh = GitHub(token, env.get("GITHUB_API_URL", "https://api.github.com"))
    try:
        action, url = upsert_comment(gh, repo_slug, pr, render_body(report.read_text(), run_url))
    except CommentError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    finally:
        gh.close()
    typer.echo(f"{action} {url}")


def make_llm(cfg, run):
    """The LLM client for `fix`; tests replace this with a fake transport."""
    from patchwarden.llm import make_client

    return make_client(cfg, run)


def _open_store(trace_dir: Path):
    from patchwarden.tracing.store import TraceStore

    if not (trace_dir / "traces.db").is_file():
        typer.echo(f"error: no trace store at {trace_dir} (run `patchwarden fix` first)", err=True)
        raise typer.Exit(2)
    return TraceStore(trace_dir)


@trace_app.command("list")
def trace_list(
    trace_dir: TraceDir = Path(".patchwarden"),
    limit: Annotated[int, typer.Option(help="How many runs, newest first.")] = 20,
) -> None:
    """Recent runs: outcome, findings, flags and cost."""
    from patchwarden.tracing.render import render_list

    store = _open_store(trace_dir)
    try:
        typer.echo(render_list(store, limit), nl=False)
    finally:
        store.close()


@trace_app.command("show")
def trace_show(
    run_id: Annotated[str, typer.Argument(help='A run id, a unique prefix, or "last".')] = "last",
    trace_dir: TraceDir = Path(".patchwarden"),
) -> None:
    """One run as a timeline tree of its steps, then its failure flags."""
    from patchwarden.tracing.render import render_show, resolve_run

    store = _open_store(trace_dir)
    try:
        rid = resolve_run(store, run_id)
        if rid is None:
            typer.echo(f"error: no run matches {run_id!r} (see `patchwarden trace list`)", err=True)
            raise typer.Exit(2)
        typer.echo(render_show(store, rid), nl=False)
    finally:
        store.close()


@trace_app.command("stats")
def trace_stats(
    trace_dir: TraceDir = Path(".patchwarden"),
    run: Annotated[str | None, typer.Option(help="Only this run (id, prefix or last).")] = None,
) -> None:
    """Flag counts, and per rule: outcomes, fix rate and LLM cost."""
    from patchwarden.tracing.render import render_stats, resolve_run

    store = _open_store(trace_dir)
    try:
        rid = None
        if run is not None:
            rid = resolve_run(store, run)
            if rid is None:
                typer.echo(f"error: no run matches {run!r}", err=True)
                raise typer.Exit(2)
        typer.echo(render_stats(store, rid), nl=False)
    finally:
        store.close()
