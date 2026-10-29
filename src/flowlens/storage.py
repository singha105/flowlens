"""DuckDB storage: one database file per repository.

Two layers live in each file. Source tables (issues, pull_requests, issue_events, ...) hold
GitHub data close to how it was fetched and are upserted by key, so re-running an ingest
is safe. Model tables (work_items, work_item_events, deploys, deployment_changes) are
derived from them by flowlens.model and rebuilt from scratch each time.

Timestamps are stored as naive UTC ``TIMESTAMP`` values and come back out as aware UTC
datetimes.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    number        INTEGER PRIMARY KEY,
    title         VARCHAR NOT NULL,
    state         VARCHAR NOT NULL,      -- OPEN | CLOSED
    state_reason  VARCHAR,               -- COMPLETED | NOT_PLANNED | DUPLICATE | REOPENED
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL,
    closed_at     TIMESTAMP,
    author        VARCHAR,               -- NULL for deleted accounts
    author_type   VARCHAR,               -- User | Bot | Mannequin | ...
    labels        VARCHAR[] NOT NULL,
    assignees     VARCHAR[] NOT NULL,
    url           VARCHAR NOT NULL,
    raw           JSON NOT NULL,         -- GraphQL node as fetched, minus the timeline
    ingested_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_requests (
    number             INTEGER PRIMARY KEY,
    title              VARCHAR NOT NULL,
    state              VARCHAR NOT NULL, -- OPEN | CLOSED | MERGED
    is_draft           BOOLEAN,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL,
    merged_at          TIMESTAMP,
    closed_at          TIMESTAMP,
    additions          INTEGER,
    deletions          INTEGER,
    author             VARCHAR,
    author_type        VARCHAR,
    base_ref_name      VARCHAR,
    merge_commit_sha   VARCHAR,
    first_commit_at    TIMESTAMP,        -- author date of the PR's first commit
    first_review_at    TIMESTAMP,        -- first submitted review by another person (no bots)
    first_reviewer     VARCHAR,
    first_approval_at  TIMESTAMP,        -- first approval by another person (no bots)
    review_count       INTEGER,          -- submitted reviews by other people (no bots)
    closing_issues     INTEGER[],        -- same-repo issues this PR closes when merged
    labels             VARCHAR[] NOT NULL,
    url                VARCHAR NOT NULL,
    raw                JSON NOT NULL,    -- GraphQL node as fetched, minus reviews
    ingested_at        TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_events (
    id             VARCHAR PRIMARY KEY,  -- GraphQL node id of the timeline event
    issue_number   INTEGER NOT NULL,
    event_type     VARCHAR NOT NULL,     -- assigned | unassigned | labeled | unlabeled |
                                         -- cross_referenced | connected | closed | reopened
    created_at     TIMESTAMP NOT NULL,
    actor          VARCHAR,
    label          VARCHAR,              -- labeled / unlabeled
    assignee       VARCHAR,              -- assigned / unassigned
    pr_number      INTEGER,              -- PR that referenced, was connected to, or closed it
    pr_repo        VARCHAR,              -- that PR's owner/name (may be another repo)
    pr_created_at  TIMESTAMP,
    will_close     BOOLEAN,              -- cross_referenced with a closing keyword
    state_reason   VARCHAR,              -- closed
    raw            JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_reviews (
    id            VARCHAR PRIMARY KEY,
    pr_number     INTEGER NOT NULL,
    author        VARCHAR,
    author_type   VARCHAR,
    state         VARCHAR NOT NULL,      -- APPROVED | CHANGES_REQUESTED | COMMENTED | ...
    submitted_at  TIMESTAMP              -- NULL while pending
);

CREATE TABLE IF NOT EXISTS releases (
    id             VARCHAR PRIMARY KEY,
    tag_name       VARCHAR NOT NULL,
    name           VARCHAR,
    url            VARCHAR,
    is_draft       BOOLEAN,
    is_prerelease  BOOLEAN,
    created_at     TIMESTAMP NOT NULL,
    published_at   TIMESTAMP,
    target_sha     VARCHAR,              -- commit the tag points at
    author         VARCHAR,
    raw            JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id            VARCHAR PRIMARY KEY,
    environment   VARCHAR,
    task          VARCHAR,
    ref_name      VARCHAR,
    sha           VARCHAR,
    creator       VARCHAR,
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL,
    latest_state  VARCHAR,
    raw           JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_statuses (
    id             VARCHAR PRIMARY KEY,
    deployment_id  VARCHAR NOT NULL,
    state          VARCHAR NOT NULL,     -- QUEUED | IN_PROGRESS | SUCCESS | FAILURE | ERROR |
                                         -- INACTIVE | ...
    created_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id              BIGINT PRIMARY KEY,
    workflow_id     BIGINT,
    workflow_name   VARCHAR,
    workflow_path   VARCHAR,
    display_title   VARCHAR,
    event           VARCHAR,
    status          VARCHAR,             -- queued | in_progress | completed | ...
    conclusion      VARCHAR,             -- success | failure | cancelled | skipped | ...
    head_sha        VARCHAR,
    head_branch     VARCHAR,
    run_attempt     INTEGER,
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    run_started_at  TIMESTAMP,
    url             VARCHAR
);

-- The commits between two consecutive deploys, from the compare API. Commits never change,
-- so a stored compare is never fetched again.
CREATE TABLE IF NOT EXISTS compares (
    id             VARCHAR PRIMARY KEY,  -- "<base sha>...<head sha>"
    base_sha       VARCHAR NOT NULL,
    head_sha       VARCHAR NOT NULL,
    status         VARCHAR NOT NULL,     -- ahead | behind | diverged | identical | not_found
    ahead_by       INTEGER,
    behind_by      INTEGER,
    total_commits  INTEGER,              -- commits in head that aren't in base
    fetched        INTEGER NOT NULL,     -- commits stored; fewer than total if GitHub cut it short
    fetched_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS compare_commits (
    id            VARCHAR PRIMARY KEY,   -- "<compare id>:<sha>"
    compare_id    VARCHAR NOT NULL,
    position      INTEGER NOT NULL,      -- 0 is the oldest
    sha           VARCHAR NOT NULL,
    message       VARCHAR NOT NULL,
    author        VARCHAR,               -- GitHub login; NULL when the email isn't linked
    authored_at   TIMESTAMP,
    committed_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS work_items (
    id              VARCHAR PRIMARY KEY, -- "issue#123" | "pr#456"
    type            VARCHAR NOT NULL,    -- issue | pr
    number          INTEGER NOT NULL,
    title           VARCHAR,
    url             VARCHAR,
    author          VARCHAR,
    author_type     VARCHAR,
    created_at      TIMESTAMP NOT NULL,
    started_at      TIMESTAMP,           -- NULL when no start signal fired
    started_signal  VARCHAR,             -- which signal set started_at
    done_at         TIMESTAMP,           -- NULL while open
    outcome         VARCHAR NOT NULL,    -- open | completed | not_planned | duplicate |
                                         -- closed_no_reason | merged | abandoned
    labels          VARCHAR[] NOT NULL,
    kinds           VARCHAR[] NOT NULL,  -- bug | incident | hotfix, from flowlens.yaml
    linked          INTEGER[] NOT NULL,  -- issue: linked PRs; PR: issues it closes
    reopen_count    INTEGER NOT NULL,
    excluded        BOOLEAN NOT NULL,
    exclude_reason  VARCHAR
);

CREATE TABLE IF NOT EXISTS work_item_events (
    work_item_id  VARCHAR NOT NULL,
    occurred_at   TIMESTAMP NOT NULL,
    kind          VARCHAR NOT NULL,      -- created | assigned | labeled | linked_pr |
                                         -- first_commit | review_* | merged | closed | ...
    actor         VARCHAR,
    detail        VARCHAR,
    source_id     VARCHAR                -- id of the source row, for tracing
);

CREATE TABLE IF NOT EXISTS deploys (
    id           VARCHAR PRIMARY KEY,
    source       VARCHAR NOT NULL,       -- releases | deployments | workflow:<name>
    name         VARCHAR,
    sha          VARCHAR,
    environment  VARCHAR,
    status       VARCHAR NOT NULL,       -- success | failure
    deployed_at  TIMESTAMP NOT NULL,
    url          VARCHAR
);

-- Which deploy shipped each merged PR: the first one whose compare range holds its merge
-- commit, or a commit that names it ("Title (#123)", as cherry-picks and squashes do).
CREATE TABLE IF NOT EXISTS deployment_changes (
    pr_number   INTEGER PRIMARY KEY,
    deploy_id   VARCHAR NOT NULL,
    commit_sha  VARCHAR NOT NULL,        -- the commit in that deploy's range that carried it
    matched_by  VARCHAR NOT NULL         -- merge_commit | pr_reference
);

CREATE SEQUENCE IF NOT EXISTS ingest_run_id;

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id           INTEGER PRIMARY KEY DEFAULT nextval('ingest_run_id'),
    repo             VARCHAR NOT NULL,
    mode             VARCHAR NOT NULL,   -- full | incremental
    status           VARCHAR NOT NULL,   -- running | success | failed
    fetch_version    INTEGER,            -- which set of fetched fields produced the data
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    since_requested  TIMESTAMP,          -- --since as given; NULL means all history
    coverage_from    TIMESTAMP,          -- oldest timestamp the database is complete from
    api_calls        INTEGER,
    api_cost         INTEGER,
    error            VARCHAR
);

CREATE TABLE IF NOT EXISTS ingest_entities (
    run_id      INTEGER NOT NULL,
    entity      VARCHAR NOT NULL,        -- issues | pull_requests | releases | deployments |
                                         -- workflow_runs:<name>
    since_used  TIMESTAMP,               -- lower bound actually used
    fetched     INTEGER NOT NULL,
    watermark   TIMESTAMP,               -- newest updated/created time seen so far
    PRIMARY KEY (run_id, entity)
);
"""

# Databases written by older versions get the newer columns added in place. The ingest
# runner sees their older fetch_version and refetches everything to fill them.
MIGRATIONS = """
ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS merge_commit_sha VARCHAR;
ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS first_approval_at TIMESTAMP;
ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS review_count INTEGER;
ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS closing_issues INTEGER[];
ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS fetch_version INTEGER;
"""


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    key: str
    # Upserts skip rows older than what's stored, judged by this column. Without one, the
    # latest fetch wins.
    version: str | None = None


TABLES: dict[str, Table] = {
    "issues": Table(
        (
            "number", "title", "state", "state_reason", "created_at", "updated_at",
            "closed_at", "author", "author_type", "labels", "assignees", "url", "raw",
            "ingested_at",
        ),
        key="number",
        version="updated_at",
    ),
    "pull_requests": Table(
        (
            "number", "title", "state", "is_draft", "created_at", "updated_at", "merged_at",
            "closed_at", "additions", "deletions", "author", "author_type", "base_ref_name",
            "merge_commit_sha", "first_commit_at", "first_review_at", "first_reviewer",
            "first_approval_at", "review_count", "closing_issues", "labels", "url", "raw",
            "ingested_at",
        ),
        key="number",
        version="updated_at",
    ),
    "issue_events": Table(
        (
            "id", "issue_number", "event_type", "created_at", "actor", "label", "assignee",
            "pr_number", "pr_repo", "pr_created_at", "will_close", "state_reason", "raw",
        ),
        key="id",
    ),
    "pr_reviews": Table(
        ("id", "pr_number", "author", "author_type", "state", "submitted_at"), key="id"
    ),
    "releases": Table(
        (
            "id", "tag_name", "name", "url", "is_draft", "is_prerelease", "created_at",
            "published_at", "target_sha", "author", "raw",
        ),
        key="id",
    ),
    "deployments": Table(
        (
            "id", "environment", "task", "ref_name", "sha", "creator", "created_at",
            "updated_at", "latest_state", "raw",
        ),
        key="id",
        version="updated_at",
    ),
    "deployment_statuses": Table(("id", "deployment_id", "state", "created_at"), key="id"),
    "workflow_runs": Table(
        (
            "id", "workflow_id", "workflow_name", "workflow_path", "display_title", "event",
            "status", "conclusion", "head_sha", "head_branch", "run_attempt", "created_at",
            "updated_at", "run_started_at", "url",
        ),
        key="id",
        version="updated_at",
    ),
    "compares": Table(
        (
            "id", "base_sha", "head_sha", "status", "ahead_by", "behind_by", "total_commits",
            "fetched", "fetched_at",
        ),
        key="id",
    ),
    "compare_commits": Table(
        (
            "id", "compare_id", "position", "sha", "message", "author", "authored_at",
            "committed_at",
        ),
        key="id",
    ),
    "work_items": Table(
        (
            "id", "type", "number", "title", "url", "author", "author_type", "created_at",
            "started_at", "started_signal", "done_at", "outcome", "labels", "kinds", "linked",
            "reopen_count", "excluded", "exclude_reason",
        ),
        key="id",
    ),
    "work_item_events": Table(
        ("work_item_id", "occurred_at", "kind", "actor", "detail", "source_id"), key="work_item_id"
    ),
    "deploys": Table(
        ("id", "source", "name", "sha", "environment", "status", "deployed_at", "url"),
        key="id",
    ),
    "deployment_changes": Table(
        ("pr_number", "deploy_id", "commit_sha", "matched_by"), key="pr_number"
    ),
}  # fmt: skip

MODEL_TABLES = ("work_items", "work_item_events", "deploys", "deployment_changes")
INSERT_CHUNK = 500


@dataclass(frozen=True)
class PreviousRun:
    """What the last successful ingest left behind, used to plan the next one."""

    run_id: int
    fetch_version: int | None
    coverage_from: datetime | None
    finished_at: datetime | None
    watermarks: dict[str, datetime | None]


def db_path_for(data_dir: Path, owner: str, name: str) -> Path:
    return data_dir / f"{owner}__{name}.duckdb"


class Store:
    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        if str(path) != ":memory:" and not read_only:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = duckdb.connect(str(path), read_only=read_only)
        if not read_only:
            self.con.execute(SCHEMA)
            self.con.execute(MIGRATIONS)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def upsert(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Insert or update rows by the table's key in one statement; returns rows written.

        A batch can hold the same key twice (pagination may repeat an item that was
        updated mid-crawl), so only the newest copy of each key is kept.
        """
        spec = TABLES[table]
        latest: dict[Any, dict[str, Any]] = {}
        for row in rows:
            kept = latest.get(row[spec.key])
            if kept is None or spec.version is None or row[spec.version] >= kept[spec.version]:
                latest[row[spec.key]] = row
        if not latest:
            return 0

        ingested_at = _to_db(datetime.now(UTC))
        columns = spec.columns
        placeholders = "(" + ", ".join(["?"] * len(columns)) + ")"
        updates = ", ".join(f"{col} = excluded.{col}" for col in columns if col != spec.key)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES {', '.join([placeholders] * len(latest))} "
            f"ON CONFLICT ({spec.key}) DO UPDATE SET {updates}"
        )
        if spec.version:
            sql += f" WHERE excluded.{spec.version} >= {table}.{spec.version}"
        params = [
            ingested_at if col == "ingested_at" else _to_db(row[col])
            for row in latest.values()
            for col in columns
        ]
        self.con.execute(sql, params)
        return len(latest)

    def replace(self, rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
        """Swap the contents of model tables in one transaction."""
        self.con.begin()
        try:
            for table, rows in rows_by_table.items():
                if table not in MODEL_TABLES:
                    raise ValueError(f"{table} is not a model table")
                columns = TABLES[table].columns
                self.con.execute(f"DELETE FROM {table}")
                placeholders = "(" + ", ".join(["?"] * len(columns)) + ")"
                for start in range(0, len(rows), INSERT_CHUNK):
                    chunk = rows[start : start + INSERT_CHUNK]
                    self.con.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) "
                        f"VALUES {', '.join([placeholders] * len(chunk))}",
                        [_to_db(row[col]) for row in chunk for col in columns],
                    )
        except BaseException:
            self.con.rollback()
            raise
        self.con.commit()

    def rows(self, table: str) -> list[dict[str, Any]]:
        """Every row of a table as dicts with aware UTC datetimes (``raw`` left out)."""
        columns = [col for col in TABLES[table].columns if col not in {"raw", "ingested_at"}]
        result = self.con.execute(f"SELECT {', '.join(columns)} FROM {table}")
        return [
            {col: _from_db(value) for col, value in zip(columns, row, strict=True)}
            for row in result.fetchall()
        ]

    def count(self, table: str) -> int:
        if table not in TABLES and table != "ingest_runs":
            raise ValueError(f"unknown table {table!r}")
        (total,) = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()
        return total

    def last_successful_run(self, repo: str) -> PreviousRun | None:
        row = self.con.execute(
            """
            SELECT run_id, fetch_version, coverage_from, finished_at
            FROM ingest_runs
            WHERE repo = ? AND status = 'success'
            ORDER BY run_id DESC
            LIMIT 1
            """,
            [repo],
        ).fetchone()
        if row is None:
            return None
        run_id, fetch_version, coverage_from, finished_at = row
        entities = self.con.execute(
            "SELECT entity, watermark FROM ingest_entities WHERE run_id = ?", [run_id]
        ).fetchall()
        return PreviousRun(
            run_id=run_id,
            fetch_version=fetch_version,
            coverage_from=_from_db(coverage_from),
            finished_at=_from_db(finished_at),
            watermarks={entity: _from_db(watermark) for entity, watermark in entities},
        )

    def start_run(
        self,
        repo: str,
        *,
        mode: str,
        fetch_version: int,
        since_requested: datetime | None,
        coverage_from: datetime | None,
    ) -> int:
        (run_id,) = self.con.execute(
            """
            INSERT INTO ingest_runs
                (repo, mode, status, fetch_version, started_at, since_requested, coverage_from)
            VALUES (?, ?, 'running', ?, ?, ?, ?)
            RETURNING run_id
            """,
            [
                repo,
                mode,
                fetch_version,
                _to_db(datetime.now(UTC)),
                _to_db(since_requested),
                _to_db(coverage_from),
            ],
        ).fetchone()
        return run_id

    def record_entity(
        self,
        run_id: int,
        entity: str,
        *,
        since_used: datetime | None,
        fetched: int,
        watermark: datetime | None,
    ) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO ingest_entities VALUES (?, ?, ?, ?, ?)",
            [run_id, entity, _to_db(since_used), fetched, _to_db(watermark)],
        )

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        api_calls: int,
        api_cost: int,
        error: str | None = None,
    ) -> None:
        self.con.execute(
            """
            UPDATE ingest_runs
            SET status = ?, finished_at = ?, api_calls = ?, api_cost = ?, error = ?
            WHERE run_id = ?
            """,
            [status, _to_db(datetime.now(UTC)), api_calls, api_cost, error, run_id],
        )


def _to_db(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}; FlowLens only stores UTC")
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return value


def _from_db(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC)
    return value
