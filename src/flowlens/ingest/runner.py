"""Incremental ingest: decide what to fetch, stream it into DuckDB, record the run.

``--since`` sets how far back the database should reach. After a successful run, the next
run only asks GitHub for items changed after each entity's high-water mark, as long as the
database already reaches back as far as the new ``--since`` and was filled by the current
set of queries. Otherwise (or with ``--full``) everything is fetched again from ``--since``.

Given the settings, the run then compares each deploy from the configured source with the
one before it, so the model can tell which PRs each deploy shipped. Commits never change,
so a range is fetched once and kept.
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flowlens.config import Config
from flowlens.ingest.client import GitHubClient, GitHubNotFoundError, token_from_gh
from flowlens.ingest.delivery import (
    compare_commit_rows,
    compare_row,
    deployment_row,
    deployment_status_rows,
    fetch_compare,
    fetch_deployments,
    fetch_releases,
    fetch_workflow_runs,
    find_workflow,
    release_row,
    workflow_run_row,
)
from flowlens.ingest.github import (
    fetch_issues,
    fetch_pull_requests,
    issue_event_rows,
    issue_row,
    parse_ts,
    pull_request_row,
    review_rows,
)
from flowlens.model.changes import deploy_ranges
from flowlens.model.deploys import resolve_deploys
from flowlens.storage import PreviousRun, Store, db_path_for

# Bump when what gets fetched changes: older data then gets refetched in full.
# 2: timelines, reviews, releases, deployments, workflow runs. 3: issue pages of 25, since
# pages of 50 lost timeline events.
FETCH_VERSION = 3

# Re-read a few minutes before the last watermark to absorb clock skew and items whose
# update landed in GitHub's index after we read past them. Upserts make the overlap free.
WATERMARK_OVERLAP = timedelta(minutes=5)
# Releases, deployments, and workflow runs are read by creation time but keep changing
# after it (a draft is published, a status goes from in_progress to success), so recent
# ones are re-read.
REFRESH_WINDOW = timedelta(days=3)

Node = dict[str, Any]


@dataclass(frozen=True)
class Entity:
    """One stream of things to fetch, with how to store it and where to resume."""

    name: str
    fetch: Callable[[datetime | None], Iterator[list[Node]]]
    write: Callable[[Store, list[Node]], None]
    stamp: str  # node field that orders the stream and becomes the watermark
    lookback: timedelta = WATERMARK_OVERLAP


@dataclass(frozen=True)
class RunPlan:
    mode: str  # "full" | "incremental"
    coverage_from: datetime | None
    since: dict[str, datetime | None]
    previous: PreviousRun | None = None


@dataclass(frozen=True)
class RangeTally:
    """The compare ranges behind the deploys from the configured source."""

    source: str
    deploys: int  # successful deploys
    needed: int  # deploys with an earlier, different commit to compare with
    fetched: int  # compares fetched by this run; the rest were already stored
    commits: int  # commits those compares returned
    not_found: int  # compares GitHub couldn't make (a commit no longer exists)


@dataclass(frozen=True)
class IngestSummary:
    repo: str
    db_path: Path
    mode: str
    since: dict[str, datetime | None]
    fetched: dict[str, int]
    first_updated: datetime | None
    last_updated: datetime | None
    api_calls: int
    api_cost: int
    rate_remaining: int | None
    rate_limit: int | None
    seconds: float
    stored: dict[str, int] = field(default_factory=dict)
    ranges: RangeTally | None = None


def parse_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.strip().partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"expected owner/name, got {repo!r}")
    return owner, name


def plan_run(
    previous: PreviousRun | None,
    *,
    since: datetime | None,
    full: bool,
    lookbacks: dict[str, timedelta],
) -> RunPlan:
    """Choose a full or incremental fetch and the ``since`` bound for each entity."""
    if (
        full
        or previous is None
        or previous.fetch_version != FETCH_VERSION
        or not _covers(previous.coverage_from, since)
    ):
        return RunPlan("full", since, dict.fromkeys(lookbacks, since))
    floor = previous.coverage_from
    return RunPlan(
        "incremental",
        floor,
        {
            entity: _resume_from(previous.watermarks.get(entity), floor, lookback)
            for entity, lookback in lookbacks.items()
        },
        previous,
    )


def _covers(coverage_from: datetime | None, since: datetime | None) -> bool:
    """Does a database complete from ``coverage_from`` already reach back to ``since``?"""
    if coverage_from is None:
        return True
    return since is not None and coverage_from <= since


def _resume_from(
    watermark: datetime | None, floor: datetime | None, lookback: timedelta
) -> datetime | None:
    if watermark is None:
        return floor  # never fetched (a newly configured workflow), or nothing found yet
    resume = watermark - lookback
    return max(resume, floor) if floor is not None else resume


def build_entities(
    client: GitHubClient, owner: str, name: str, workflows: list[dict[str, Any]]
) -> list[Entity]:
    repo = f"{owner}/{name}"

    def write_issues(store: Store, nodes: list[Node]) -> None:
        store.upsert("issues", [issue_row(node) for node in nodes])
        store.upsert("issue_events", [row for node in nodes for row in issue_event_rows(node)])

    def write_pull_requests(store: Store, nodes: list[Node]) -> None:
        store.upsert("pull_requests", [pull_request_row(node, repo) for node in nodes])
        store.upsert("pr_reviews", [row for node in nodes for row in review_rows(node)])

    def write_deployments(store: Store, nodes: list[Node]) -> None:
        store.upsert("deployments", [deployment_row(node) for node in nodes])
        store.upsert(
            "deployment_statuses", [row for node in nodes for row in deployment_status_rows(node)]
        )

    entities = [
        Entity(
            "issues",
            lambda since: fetch_issues(client, owner, name, since),
            write_issues,
            "updatedAt",
        ),
        Entity(
            "pull_requests",
            lambda since: fetch_pull_requests(client, owner, name, since),
            write_pull_requests,
            "updatedAt",
        ),
        Entity(
            "releases",
            lambda since: fetch_releases(client, owner, name, since),
            lambda store, nodes: store.upsert("releases", [release_row(n) for n in nodes]),
            "createdAt",
            REFRESH_WINDOW,
        ),
        Entity(
            "deployments",
            lambda since: fetch_deployments(client, owner, name, since),
            write_deployments,
            "createdAt",
            REFRESH_WINDOW,
        ),
    ]
    for workflow in workflows:
        entities.append(
            Entity(
                f"workflow_runs:{workflow['name']}",
                lambda since, workflow=workflow: fetch_workflow_runs(
                    client, owner, name, workflow, since
                ),
                lambda store, runs: store.upsert(
                    "workflow_runs", [workflow_run_row(run) for run in runs]
                ),
                "created_at",
                REFRESH_WINDOW,
            )
        )
    return entities


@dataclass
class _Tally:
    fetched: int = 0
    first: datetime | None = None
    last: datetime | None = None

    def add(self, stamps: list[datetime]) -> None:
        self.fetched += len(stamps)
        if stamps:
            self.first = min(stamps) if self.first is None else min(self.first, *stamps)
            self.last = max(stamps) if self.last is None else max(self.last, *stamps)


def _drain(entity: Entity, since: datetime | None, store: Store, progress: Callable) -> _Tally:
    """Write each page as it arrives, so a failed run keeps what it already fetched."""
    tally = _Tally()
    label = entity.name.replace("_", " ")
    for page_number, nodes in enumerate(entity.fetch(since), start=1):
        entity.write(store, nodes)
        tally.add([parse_ts(node[entity.stamp]) for node in nodes])
        progress(f"{label}: page {page_number}, {tally.fetched} fetched")
    return tally


def fetch_deploy_ranges(
    client: GitHubClient,
    store: Store,
    owner: str,
    name: str,
    config: Config,
    progress: Callable[[str], None],
) -> RangeTally:
    """Compare every successful deploy with the one before it, unless already stored."""
    deploys = resolve_deploys(
        config,
        releases=store.rows("releases"),
        deployments=store.rows("deployments"),
        statuses=store.rows("deployment_statuses"),
        runs=store.rows("workflow_runs"),
    )
    ranges = {r.compare_id: r for r in deploy_ranges(deploys) if r.compare_id}
    stored = {row["id"] for row in store.rows("compares")}
    missing = [r for range_id, r in ranges.items() if range_id not in stored]
    commits = not_found = 0
    for number, deploy_range in enumerate(missing, start=1):
        base, head = deploy_range.base_sha, deploy_range.head_sha
        assert base and head
        try:
            summary, fetched = fetch_compare(client, owner, name, base, head)
        except GitHubNotFoundError:
            summary, fetched = None, []
            not_found += 1
        # Commits first: a compares row means its commits are all stored.
        store.upsert("compare_commits", compare_commit_rows(base, head, fetched))
        store.upsert("compares", [compare_row(base, head, summary, fetched, client.now())])
        commits += len(fetched)
        progress(f"deploy ranges: {number} of {len(missing)} compared, {commits} commits")
    return RangeTally(
        source=config.deployment_source,
        deploys=sum(deploy.status == "success" for deploy in deploys),
        needed=len(ranges),
        fetched=len(missing),
        commits=commits,
        not_found=not_found,
    )


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
    workflows: tuple[str, ...] = (),
    config: Config | None = None,
    data_dir: Path = Path("data"),
    client: GitHubClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> IngestSummary:
    """Fetch issues, PRs, releases, deployments, and the named workflows' runs for
    ``owner/name`` into ``data_dir/<owner>__<name>.duckdb``; with ``config``, also the
    commits each deploy from its ``deployment_source`` shipped."""
    owner, name = parse_repo(repo)
    repo = f"{owner}/{name}"
    progress = progress or (lambda _message: None)
    started = time.monotonic()
    owns_client = client is None
    if client is None:
        client = GitHubClient(token_from_gh(), log=progress)

    db_path = db_path_for(data_dir, owner, name)
    try:
        # "Build Release" and "release.yml" can name the same workflow; fetch it once.
        resolved = {
            workflow["id"]: workflow
            for workflow in (find_workflow(client, owner, name, wanted) for wanted in workflows)
        }
        entities = build_entities(client, owner, name, list(resolved.values()))
        with Store(db_path) as store:
            plan = plan_run(
                store.last_successful_run(repo),
                since=since,
                full=full,
                lookbacks={entity.name: entity.lookback for entity in entities},
            )
            run_id = store.start_run(
                repo,
                mode=plan.mode,
                fetch_version=FETCH_VERSION,
                since_requested=since,
                coverage_from=plan.coverage_from,
            )
            tallies: dict[str, _Tally] = {}
            ranges: RangeTally | None = None
            previous = plan.previous
            try:
                for entity in entities:
                    tally = _drain(entity, plan.since[entity.name], store, progress)
                    tallies[entity.name] = tally
                    carried = previous.watermarks.get(entity.name) if previous else None
                    store.record_entity(
                        run_id,
                        entity.name,
                        since_used=plan.since[entity.name],
                        fetched=tally.fetched,
                        watermark=_later(carried, tally.last),
                    )
                if config is not None:
                    ranges = fetch_deploy_ranges(client, store, owner, name, config, progress)
                    store.record_entity(
                        run_id,
                        "deploy_ranges",
                        since_used=None,
                        fetched=ranges.fetched,
                        watermark=None,
                    )
            except BaseException as exc:
                # Only successful runs are planned from, so the last good run's watermarks
                # stay in charge and the next run refetches whatever this one didn't finish.
                store.finish_run(
                    run_id,
                    status="failed",
                    api_calls=client.calls,
                    api_cost=client.cost,
                    error=str(exc) or type(exc).__name__,
                )
                raise
            store.finish_run(run_id, status="success", api_calls=client.calls, api_cost=client.cost)
            stored = {
                table: store.count(table)
                for table in ("issues", "pull_requests", "releases", "deployments", "workflow_runs")
            }
    finally:
        if owns_client:
            client.close()

    work = [tallies["issues"], tallies["pull_requests"]]
    rate = client.rate_limit
    return IngestSummary(
        repo=repo,
        db_path=db_path,
        mode=plan.mode,
        since=plan.since,
        fetched={entity: tally.fetched for entity, tally in tallies.items()},
        first_updated=_earlier(work[0].first, work[1].first),
        last_updated=_later(work[0].last, work[1].last),
        api_calls=client.calls,
        api_cost=client.cost,
        rate_remaining=rate.remaining if rate else None,
        rate_limit=rate.limit if rate else None,
        seconds=time.monotonic() - started,
        stored=stored,
        ranges=ranges,
    )
