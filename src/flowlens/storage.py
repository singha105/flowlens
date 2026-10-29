"""DuckDB storage: one database file per repository.

Timestamps are stored as naive UTC ``TIMESTAMP`` values and come back out as aware UTC
datetimes. Rows are upserted by issue/PR number, and an older snapshot never overwrites a
newer one, so re-running an ingest is safe.
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
    raw           JSON NOT NULL,         -- GraphQL node as fetched
    ingested_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_requests (
    number           INTEGER PRIMARY KEY,
    title            VARCHAR NOT NULL,
    state            VARCHAR NOT NULL,   -- OPEN | CLOSED | MERGED
    is_draft         BOOLEAN,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    merged_at        TIMESTAMP,
    closed_at        TIMESTAMP,
    additions        INTEGER,
    deletions        INTEGER,
    author           VARCHAR,
    author_type      VARCHAR,
    base_ref_name    VARCHAR,
    first_commit_at  TIMESTAMP,          -- author date of the PR's first commit
    first_review_at  TIMESTAMP,          -- first submitted review by someone else
    first_reviewer   VARCHAR,
    labels           VARCHAR[] NOT NULL,
    url              VARCHAR NOT NULL,
    raw              JSON NOT NULL,
    ingested_at      TIMESTAMP NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS ingest_run_id;

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id            INTEGER PRIMARY KEY DEFAULT nextval('ingest_run_id'),
    repo              VARCHAR NOT NULL,
    mode              VARCHAR NOT NULL,  -- full | incremental
    status            VARCHAR NOT NULL,  -- running | success | failed
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP,
    since_requested   TIMESTAMP,         -- --since as given; NULL means all history
    coverage_from     TIMESTAMP,         -- oldest updated_at the database is complete from
    issues_since      TIMESTAMP,         -- lower bound actually sent for issues
    prs_since         TIMESTAMP,         -- lower bound actually used for pull requests
    issues_fetched    INTEGER,
    prs_fetched       INTEGER,
    issues_watermark  TIMESTAMP,         -- newest issue updated_at seen so far
    prs_watermark     TIMESTAMP,         -- newest PR updated_at seen so far
    api_calls         INTEGER,
    api_cost          INTEGER,
    error             VARCHAR
);
"""

ISSUE_COLUMNS = (
    "number",
    "title",
    "state",
    "state_reason",
    "created_at",
    "updated_at",
    "closed_at",
    "author",
    "author_type",
    "labels",
    "assignees",
    "url",
    "raw",
    "ingested_at",
)

PULL_REQUEST_COLUMNS = (
    "number",
    "title",
    "state",
    "is_draft",
    "created_at",
    "updated_at",
    "merged_at",
    "closed_at",
    "additions",
    "deletions",
    "author",
    "author_type",
    "base_ref_name",
    "first_commit_at",
    "first_review_at",
    "first_reviewer",
    "labels",
    "url",
    "raw",
    "ingested_at",
)


@dataclass(frozen=True)
class PreviousRun:
    """What the last successful ingest left behind, used to plan the next one."""

    run_id: int
    coverage_from: datetime | None
    issues_watermark: datetime | None
    prs_watermark: datetime | None


def db_path_for(data_dir: Path, owner: str, name: str) -> Path:
    return data_dir / f"{owner}__{name}.duckdb"


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def upsert_issues(self, rows: list[dict[str, Any]]) -> int:
        return self._upsert("issues", ISSUE_COLUMNS, rows)

    def upsert_pull_requests(self, rows: list[dict[str, Any]]) -> int:
        return self._upsert("pull_requests", PULL_REQUEST_COLUMNS, rows)

    def _upsert(self, table: str, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> int:
        """Insert or update rows by number in one statement; returns rows written.

        A batch can hold the same number twice (pagination may repeat an item that was
        updated mid-crawl), so only the newest copy of each number is kept.
        """
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            kept = latest.get(row["number"])
            if kept is None or row["updated_at"] >= kept["updated_at"]:
                latest[row["number"]] = row
        if not latest:
            return 0

        ingested_at = _to_db(datetime.now(UTC))
        placeholders = "(" + ", ".join(["?"] * len(columns)) + ")"
        updates = ", ".join(f"{col} = excluded.{col}" for col in columns if col != "number")
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES {', '.join([placeholders] * len(latest))} "
            f"ON CONFLICT (number) DO UPDATE SET {updates} "
            f"WHERE excluded.updated_at >= {table}.updated_at"
        )
        params = [
            ingested_at if col == "ingested_at" else _to_db(row[col])
            for row in latest.values()
            for col in columns
        ]
        self.con.execute(sql, params)
        return len(latest)

    def count(self, table: str) -> int:
        if table not in {"issues", "pull_requests", "ingest_runs"}:
            raise ValueError(f"unknown table {table!r}")
        (total,) = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()
        return total

    def last_successful_run(self, repo: str) -> PreviousRun | None:
        row = self.con.execute(
            """
            SELECT run_id, coverage_from, issues_watermark, prs_watermark
            FROM ingest_runs
            WHERE repo = ? AND status = 'success'
            ORDER BY run_id DESC
            LIMIT 1
            """,
            [repo],
        ).fetchone()
        if row is None:
            return None
        run_id, coverage_from, issues_watermark, prs_watermark = row
        return PreviousRun(
            run_id=run_id,
            coverage_from=_from_db(coverage_from),
            issues_watermark=_from_db(issues_watermark),
            prs_watermark=_from_db(prs_watermark),
        )

    def start_run(
        self,
        repo: str,
        *,
        mode: str,
        since_requested: datetime | None,
        coverage_from: datetime | None,
        issues_since: datetime | None,
        prs_since: datetime | None,
    ) -> int:
        (run_id,) = self.con.execute(
            """
            INSERT INTO ingest_runs
                (repo, mode, status, started_at, since_requested, coverage_from,
                 issues_since, prs_since)
            VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
            RETURNING run_id
            """,
            [
                repo,
                mode,
                _to_db(datetime.now(UTC)),
                _to_db(since_requested),
                _to_db(coverage_from),
                _to_db(issues_since),
                _to_db(prs_since),
            ],
        ).fetchone()
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        issues_fetched: int,
        prs_fetched: int,
        issues_watermark: datetime | None,
        prs_watermark: datetime | None,
        api_calls: int,
        api_cost: int,
        error: str | None = None,
    ) -> None:
        self.con.execute(
            """
            UPDATE ingest_runs
            SET status = ?, finished_at = ?, issues_fetched = ?, prs_fetched = ?,
                issues_watermark = ?, prs_watermark = ?, api_calls = ?, api_cost = ?,
                error = ?
            WHERE run_id = ?
            """,
            [
                status,
                _to_db(datetime.now(UTC)),
                issues_fetched,
                prs_fetched,
                _to_db(issues_watermark),
                _to_db(prs_watermark),
                api_calls,
                api_cost,
                error,
                run_id,
            ],
        )


def _to_db(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}; FlowLens only stores UTC")
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return value


def _from_db(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None else None
