"""Incremental ingest: decide what to fetch, stream it into DuckDB, record the run.

``--since`` sets how far back the database should reach. After a successful run, the next
run only asks GitHub for items updated after that run's high-water mark, as long as the
database already reaches back as far as the new ``--since``. Asking for older history
than the database holds (or passing ``--full``) triggers a full fetch from ``--since``.
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flowlens.ingest.github import (
    GraphQLClient,
    fetch_issues,
    fetch_pull_requests,
    issue_row,
    pull_request_row,
    token_from_gh,
)
from flowlens.storage import PreviousRun, Store, db_path_for

# Re-read a few minutes before the last watermark to absorb clock skew and items whose
# update landed in GitHub's index after we read past them. Upserts make the overlap free.
WATERMARK_OVERLAP = timedelta(minutes=5)


@dataclass(frozen=True)
class RunPlan:
    mode: str  # "full" | "incremental"
    coverage_from: datetime | None
    issues_since: datetime | None
    prs_since: datetime | None
    previous: PreviousRun | None = None


@dataclass(frozen=True)
class IngestSummary:
    repo: str
    db_path: Path
    mode: str
    issues_since: datetime | None
    prs_since: datetime | None
    issues_fetched: int
    prs_fetched: int
    first_updated: datetime | None
    last_updated: datetime | None
    api_calls: int
    api_cost: int
    rate_remaining: int | None
    rate_limit: int | None
    seconds: float
    issues_stored: int
    prs_stored: int


def parse_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.strip().partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"expected owner/name, got {repo!r}")
    return owner, name


def plan_run(previous: PreviousRun | None, *, since: datetime | None, full: bool) -> RunPlan:
    """Choose a full or incremental fetch and the ``since`` bound for each entity."""
    if full or previous is None or not _covers(previous.coverage_from, since):
        return RunPlan("full", since, since, since)
    floor = previous.coverage_from
    return RunPlan(
        "incremental",
        floor,
        _resume_from(previous.issues_watermark, floor),
        _resume_from(previous.prs_watermark, floor),
        previous,
    )


def _covers(coverage_from: datetime | None, since: datetime | None) -> bool:
    """Does a database complete from ``coverage_from`` already reach back to ``since``?"""
    if coverage_from is None:
        return True
    return since is not None and coverage_from <= since


def _resume_from(watermark: datetime | None, floor: datetime | None) -> datetime | None:
    if watermark is None:
        return floor
    resume = watermark - WATERMARK_OVERLAP
    return max(resume, floor) if floor is not None else resume


@dataclass
class _Tally:
    fetched: int = 0
    first_updated: datetime | None = None
    last_updated: datetime | None = None

    def add(self, rows: list[dict[str, Any]]) -> None:
        self.fetched += len(rows)
        for row in rows:
            updated = row["updated_at"]
            if self.first_updated is None or updated < self.first_updated:
                self.first_updated = updated
            if self.last_updated is None or updated > self.last_updated:
                self.last_updated = updated


def _drain(
    label: str,
    pages: Iterator[list[dict[str, Any]]],
    to_row: Callable[[dict[str, Any]], dict[str, Any]],
    upsert: Callable[[list[dict[str, Any]]], int],
    progress: Callable[[str], None],
) -> _Tally:
    """Write each page as it arrives, so a failed run keeps what it already fetched."""
    tally = _Tally()
    for page_number, nodes in enumerate(pages, start=1):
        rows = [to_row(node) for node in nodes]
        upsert(rows)
        tally.add(rows)
        progress(f"{label}: page {page_number}, {tally.fetched} fetched")
    return tally


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


def _earlier(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return min(a, b)


def ingest_repo(
    repo: str,
    *,
    since: datetime | None = None,
    full: bool = False,
    data_dir: Path = Path("data"),
    client: GraphQLClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> IngestSummary:
    """Fetch issues and PRs for ``owner/name`` into ``data_dir/<owner>__<name>.duckdb``."""
    owner, name = parse_repo(repo)
    repo = f"{owner}/{name}"
    progress = progress or (lambda _message: None)
    started = time.monotonic()
    owns_client = client is None
    if client is None:
        client = GraphQLClient(token_from_gh(), log=progress)

    db_path = db_path_for(data_dir, owner, name)
    try:
        with Store(db_path) as store:
            plan = plan_run(store.last_successful_run(repo), since=since, full=full)
            run_id = store.start_run(
                repo,
                mode=plan.mode,
                since_requested=since,
                coverage_from=plan.coverage_from,
                issues_since=plan.issues_since,
                prs_since=plan.prs_since,
            )
            issues = _Tally()
            prs = _Tally()
            try:
                issues = _drain(
                    "issues",
                    fetch_issues(client, owner, name, plan.issues_since),
                    issue_row,
                    store.upsert_issues,
                    progress,
                )
                prs = _drain(
                    "pull requests",
                    fetch_pull_requests(client, owner, name, plan.prs_since),
                    pull_request_row,
                    store.upsert_pull_requests,
                    progress,
                )
            except BaseException as exc:
                # The watermarks stay where the last good run left them, so the next run
                # refetches whatever this one didn't finish.
                store.finish_run(
                    run_id,
                    status="failed",
                    issues_fetched=issues.fetched,
                    prs_fetched=prs.fetched,
                    issues_watermark=None,
                    prs_watermark=None,
                    api_calls=client.calls,
                    api_cost=client.cost,
                    error=str(exc) or type(exc).__name__,
                )
                raise

            previous = plan.previous
            store.finish_run(
                run_id,
                status="success",
                issues_fetched=issues.fetched,
                prs_fetched=prs.fetched,
                issues_watermark=_later(
                    previous.issues_watermark if previous else None, issues.last_updated
                ),
                prs_watermark=_later(
                    previous.prs_watermark if previous else None, prs.last_updated
                ),
                api_calls=client.calls,
                api_cost=client.cost,
            )
            issues_stored = store.count("issues")
            prs_stored = store.count("pull_requests")
    finally:
        if owns_client:
            client.close()

    rate = client.rate_limit
    return IngestSummary(
        repo=repo,
        db_path=db_path,
        mode=plan.mode,
        issues_since=plan.issues_since,
        prs_since=plan.prs_since,
        issues_fetched=issues.fetched,
        prs_fetched=prs.fetched,
        first_updated=_earlier(issues.first_updated, prs.first_updated),
        last_updated=_later(issues.last_updated, prs.last_updated),
        api_calls=client.calls,
        api_cost=client.cost,
        rate_remaining=rate.remaining if rate else None,
        rate_limit=rate.limit if rate else None,
        seconds=time.monotonic() - started,
        issues_stored=issues_stored,
        prs_stored=prs_stored,
    )
