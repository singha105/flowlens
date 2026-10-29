"""The ``flowlens`` command."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from flowlens import __version__
from flowlens.config import ConfigError, load_config
from flowlens.ingest.client import GitHubError
from flowlens.ingest.runner import IngestSummary, ingest_repo, parse_repo
from flowlens.model.build import ModelCounts, build_model
from flowlens.quality import build_report, format_report
from flowlens.storage import Store, db_path_for

DEFAULT_DATA_DIR = Path("data")

app = typer.Typer(
    help="Flow metrics, delivery forecasts, and DORA metrics from a GitHub repo's history.",
    no_args_is_help=True,
    add_completion=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"flowlens {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """FlowLens command line."""


def parse_since(value: str | None) -> datetime | None:
    """Parse ``2025-10-01`` or a full ISO datetime; naive values are taken as UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not an ISO date like 2025-10-01") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _check_repo(value: str) -> str:
    try:
        parse_repo(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return value


RepoOption = Annotated[str, typer.Option(help="Repository as owner/name.", callback=_check_repo)]
DataDirOption = Annotated[Path, typer.Option(help="Directory for the per-repo DuckDB files.")]
ConfigOption = Annotated[
    Path | None,
    typer.Option(help="Settings file. Defaults to ./flowlens.yaml when it exists."),
]


@app.command()
def ingest(
    repo: RepoOption,
    since: Annotated[
        str | None,
        typer.Option(help="Oldest history to keep: items updated on or after this date (UTC)."),
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Ignore earlier runs and refetch from --since.")
    ] = False,
    workflow: Annotated[
        list[str] | None,
        typer.Option(help="Also ingest this Actions workflow's runs (name or file). Repeatable."),
    ] = None,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
    config: ConfigOption = None,
) -> None:
    """Fetch issues, PRs, releases, deployments, and workflow runs, then rebuild the model."""
    since_at = parse_since(since)
    settings = _load_config(config, repo)
    workflows = tuple(dict.fromkeys([*settings.ingest_workflows, *(workflow or [])]))
    try:
        summary = ingest_repo(
            repo,
            since=since_at,
            full=full,
            workflows=workflows,
            data_dir=data_dir,
            progress=lambda message: typer.echo(f"  {message}", err=True),
        )
    except GitHubError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    with Store(summary.db_path) as store:
        counts = build_model(store, settings, summary.repo)
    typer.echo(format_summary(summary, counts))


@app.command()
def quality(
    repo: RepoOption,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
    config: ConfigOption = None,
) -> None:
    """Report how much of the ingested history FlowLens can measure."""
    settings = _load_config(config, repo)
    owner, name = parse_repo(repo)
    path = db_path_for(data_dir, owner, name)
    if not path.exists():
        typer.echo(
            f"error: no data for {repo} in {data_dir}. Run `flowlens ingest` first.", err=True
        )
        raise typer.Exit(1)
    with Store(path) as store:
        build_model(store, settings, f"{owner}/{name}")
        report = build_report(store, settings, f"{owner}/{name}")
    typer.echo(format_report(report))


def _load_config(path: Path | None, repo: str):
    try:
        return load_config(path, repo)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


def format_summary(summary: IngestSummary, counts: ModelCounts | None = None) -> str:
    def since_label(value: datetime | None) -> str:
        if value is None:
            return "all history"
        return f"since {value:%Y-%m-%d %H:%M} UTC"

    if summary.first_updated and summary.last_updated:
        date_range = f"{summary.first_updated:%Y-%m-%d} → {summary.last_updated:%Y-%m-%d}"
    else:
        date_range = "nothing new"
    rate = ""
    if summary.rate_remaining is not None:
        rate = f", {summary.rate_remaining:,} of {summary.rate_limit:,} points left"

    lines = [f"FlowLens ingest · {summary.repo} ({summary.mode} run)"]
    for entity, fetched in summary.fetched.items():
        label = entity.replace("_", " ").replace("workflow runs:", "runs of ")
        lines.append(f"  {label:<26} {fetched:>6,} fetched  ({since_label(summary.since[entity])})")
    lines += [
        f"  date range     {date_range} (issues and PRs, last updated)",
        f"  API calls      {summary.api_calls:,} (GraphQL cost {summary.api_cost:,}{rate})",
        f"  time           {summary.seconds:.1f}s",
        f"  database       {summary.db_path}",
    ]
    if counts is not None:
        lines.append(
            f"  model          {counts.work_items:,} work items, {counts.events:,} events, "
            f"{counts.deploys:,} deploys"
        )
    return "\n".join(lines)
