"""The ``flowlens`` command."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from flowlens import __version__
from flowlens.ingest.github import GitHubError
from flowlens.ingest.runner import IngestSummary, ingest_repo, parse_repo

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


@app.command()
def ingest(
    repo: Annotated[str, typer.Option(help="Repository as owner/name.", callback=_check_repo)],
    since: Annotated[
        str | None,
        typer.Option(help="Oldest history to keep: items updated on or after this date (UTC)."),
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Ignore earlier runs and refetch from --since.")
    ] = False,
    data_dir: Annotated[
        Path, typer.Option(help="Directory for the per-repo DuckDB files.")
    ] = DEFAULT_DATA_DIR,
) -> None:
    """Fetch issues and pull requests into data/<owner>__<repo>.duckdb."""
    since_at = parse_since(since)
    try:
        summary = ingest_repo(
            repo,
            since=since_at,
            full=full,
            data_dir=data_dir,
            progress=lambda message: typer.echo(f"  {message}", err=True),
        )
    except GitHubError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(format_summary(summary))


def format_summary(summary: IngestSummary) -> str:
    def since_label(value: datetime | None) -> str:
        if value is None:
            return "all history"
        return f"updated since {value:%Y-%m-%d %H:%M} UTC"

    if summary.first_updated and summary.last_updated:
        date_range = f"{summary.first_updated:%Y-%m-%d} → {summary.last_updated:%Y-%m-%d}"
    else:
        date_range = "nothing new"
    rate = ""
    if summary.rate_remaining is not None:
        rate = f", {summary.rate_remaining:,} of {summary.rate_limit:,} points left"

    issues_since = since_label(summary.issues_since)
    prs_since = since_label(summary.prs_since)
    lines = [
        f"FlowLens ingest · {summary.repo} ({summary.mode} run)",
        f"  issues         {summary.issues_fetched:>6,} fetched  ({issues_since})",
        f"  pull requests  {summary.prs_fetched:>6,} fetched  ({prs_since})",
        f"  date range     {date_range} (last updated)",
        f"  API calls      {summary.api_calls:,} (rate-limit cost {summary.api_cost:,}{rate})",
        f"  time           {summary.seconds:.1f}s",
        f"  database       {summary.db_path} "
        f"({summary.issues_stored:,} issues, {summary.prs_stored:,} PRs stored)",
    ]
    return "\n".join(lines)
