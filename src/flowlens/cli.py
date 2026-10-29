"""The ``flowlens`` command."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from flowlens import __version__
from flowlens.config import ConfigError, load_config
from flowlens.ingest.client import GitHubError
from flowlens.ingest.runner import IngestSummary, ingest_repo, parse_repo
from flowlens.metrics.dora_report import (
    dora_report_for,
    failure_rows,
    format_dora,
    write_failures,
)
from flowlens.metrics.forecast import ForecastError
from flowlens.metrics.forecast_report import (
    Throughput,
    backtest_report,
    forecast_report,
    format_backtest,
    format_forecast,
    load_throughput,
)
from flowlens.metrics.report import flow_report_for, format_flow, write_report
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


class Unit(StrEnum):
    issue = "issue"
    pr = "pr"
    total = "total"


UnitOption = Annotated[
    Unit,
    typer.Option(
        "--type", help="What to count. total counts an issue and the PR that closed it twice."
    ),
]
TrialsOption = Annotated[int, typer.Option(min=100, help="Monte Carlo trials per forecast.")]
SeedOption = Annotated[int, typer.Option(help="Random seed: the same seed gives the same result.")]


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
            config=settings,
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
    path = _existing_db(data_dir, owner, name)
    with Store(path) as store:
        build_model(store, settings, f"{owner}/{name}")
        report = build_report(store, settings, f"{owner}/{name}")
    typer.echo(format_report(report))


@app.command()
def flow(
    repo: RepoOption,
    weeks: Annotated[
        int, typer.Option(min=1, help="Whole ISO weeks to measure, ending last Monday (UTC).")
    ] = 26,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
    config: ConfigOption = None,
) -> None:
    """Cycle and lead time, throughput, WIP, aging, cumulative flow, and PR flow.

    Prints a summary and writes every series to <data-dir>/out/flow_<owner>__<name>.json.
    """
    settings = _load_config(config, repo)
    owner, name = parse_repo(repo)
    path = _existing_db(data_dir, owner, name)
    with Store(path) as store:
        build_model(store, settings, f"{owner}/{name}")
        report = flow_report_for(store, f"{owner}/{name}", weeks=weeks)
    out = write_report(report, data_dir / "out" / f"flow_{owner}__{name}.json")
    typer.echo(format_flow(report))
    typer.echo(f"\nWrote {out}")


@app.command()
def forecast(
    repo: RepoOption,
    items: Annotated[
        int | None, typer.Option(min=0, help="When will this many more items be done?")
    ] = None,
    by: Annotated[
        str | None, typer.Option(help="How many items will be done by this date (YYYY-MM-DD)?")
    ] = None,
    unit: UnitOption = Unit.issue,
    history_weeks: Annotated[
        int, typer.Option(min=1, help="How many recent complete weeks to resample.")
    ] = 12,
    start: Annotated[
        str | None, typer.Option(help="Forecast from this date. Default: the next Monday.")
    ] = None,
    trials: TrialsOption = 10_000,
    seed: SeedOption = 0,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
    config: ConfigOption = None,
) -> None:
    """Monte Carlo forecast from weekly throughput: --items for "when", --by for "how many".

    Writes the full distribution to <data-dir>/out/forecast_<owner>__<name>_<type>.json.
    """
    if (items is None) == (by is None):
        typer.echo("error: give either --items (when?) or --by (how many?)", err=True)
        raise typer.Exit(2)
    by_at, start_at = parse_since(by), parse_since(start)
    throughput = _throughput(repo, unit, data_dir, config)
    try:
        report = forecast_report(
            throughput,
            items=items,
            by=by_at,
            start=start_at,
            history_weeks=history_weeks,
            trials=trials,
            seed=seed,
        )
    except ValueError as exc:  # ForecastError included
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    out = write_report(report, _out_path(data_dir, "forecast", throughput))
    typer.echo(format_forecast(report))
    typer.echo(f"\nWrote {out}")


@app.command()
def backtest(
    repo: RepoOption,
    unit: UnitOption = Unit.issue,
    origins: Annotated[
        int, typer.Option(min=1, help="How many past weeks to forecast from, per horizon.")
    ] = 52,
    trials: TrialsOption = 10_000,
    seed: SeedOption = 0,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
    config: ConfigOption = None,
) -> None:
    """Score the forecast against history: from each past week, forecast 4 and 8 weeks ahead
    with 6, 12, and 26 weeks of history, then compare with what was delivered.

    Writes every forecast to <data-dir>/out/backtest_<owner>__<name>_<type>.json.
    """
    throughput = _throughput(repo, unit, data_dir, config)
    try:
        report = backtest_report(throughput, origins=origins, trials=trials, seed=seed)
    except ForecastError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    out = write_report(report, _out_path(data_dir, "backtest", throughput))
    typer.echo(format_backtest(report))
    typer.echo(f"\nWrote {out}")


@app.command()
def dora(
    repo: RepoOption,
    weeks: Annotated[
        int, typer.Option(min=1, help="Whole ISO weeks to measure, ending last Monday (UTC).")
    ] = 26,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
    config: ConfigOption = None,
) -> None:
    """Deployment frequency, lead time for changes, change failure rate, and failed
    deployment recovery time, from the deploys flowlens.yaml's deployment_source names.

    Writes every deploy, shipped PR, and failure signal to
    <data-dir>/out/dora_<owner>__<name>.json, and the failures that counted to
    <data-dir>/out/dora_failures_<owner>__<name>.csv.
    """
    settings = _load_config(config, repo)
    owner, name = parse_repo(repo)
    path = _existing_db(data_dir, owner, name)
    with Store(path) as store:
        build_model(store, settings, f"{owner}/{name}")
        report = dora_report_for(store, settings, f"{owner}/{name}", weeks=weeks)
    out = write_report(report, data_dir / "out" / f"dora_{owner}__{name}.json")
    failures = write_failures(
        failure_rows(report), data_dir / "out" / f"dora_failures_{owner}__{name}.csv"
    )
    typer.echo(format_dora(report, failures.name))
    typer.echo(f"\nWrote {out} and {failures}")


def _throughput(repo: str, unit: Unit, data_dir: Path, config: Path | None) -> Throughput:
    settings = _load_config(config, repo)
    owner, name = parse_repo(repo)
    path = _existing_db(data_dir, owner, name)
    with Store(path) as store:
        build_model(store, settings, f"{owner}/{name}")
        return load_throughput(store, f"{owner}/{name}", unit.value)


def _out_path(data_dir: Path, kind: str, throughput: Throughput) -> Path:
    owner, name = throughput.repo.split("/")
    return data_dir / "out" / f"{kind}_{owner}__{name}_{throughput.unit}.json"


def _existing_db(data_dir: Path, owner: str, name: str) -> Path:
    path = db_path_for(data_dir, owner, name)
    if not path.exists():
        typer.echo(
            f"error: no data for {owner}/{name} in {data_dir}. Run `flowlens ingest` first.",
            err=True,
        )
        raise typer.Exit(1)
    return path


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
    if summary.ranges is not None:
        ranges = summary.ranges
        stored = ranges.needed - ranges.fetched
        missing = f", {ranges.not_found} unknown to GitHub" if ranges.not_found else ""
        lines.insert(
            -3,
            f"  deploy ranges  {ranges.fetched:,} compared ({ranges.commits:,} commits), "
            f"{stored:,} already stored{missing}; {_plural(ranges.deploys, 'deploy')} from "
            f"{ranges.source}",
        )
    if counts is not None:
        lines.append(
            f"  model          {counts.work_items:,} work items, {counts.events:,} events, "
            f"{_plural(counts.deploys, 'deploy')}, {counts.changes:,} PRs matched to the "
            "deploy that shipped them"
        )
    return "\n".join(lines)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"
