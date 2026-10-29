import copy
import json
from datetime import UTC, datetime

import pytest

from fakes import fixture_nodes
from flowlens.ingest.github import issue_row, pull_request_row
from flowlens.storage import Store, _to_db, db_path_for


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.duckdb") as store:
        yield store


def issue_rows():
    return [issue_row(node) for node in fixture_nodes("issues", 3)]


def pr_rows():
    return [pull_request_row(node) for node in fixture_nodes("pull_requests", 2)]


def snapshot(store: Store, table: str) -> list[tuple]:
    """Every column except ingested_at, which changes on each write by design."""
    columns = [
        name
        for (name,) in store.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        if name != "ingested_at"
    ]
    return store.con.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY number").fetchall()


def test_upserting_the_same_rows_twice_changes_nothing(store):
    store.upsert_issues(issue_rows())
    store.upsert_pull_requests(pr_rows())
    issues_before = snapshot(store, "issues")
    prs_before = snapshot(store, "pull_requests")

    store.upsert_issues(issue_rows())
    store.upsert_pull_requests(pr_rows())

    assert store.count("issues") == 8
    assert store.count("pull_requests") == 8
    assert snapshot(store, "issues") == issues_before
    assert snapshot(store, "pull_requests") == prs_before


def test_a_newer_snapshot_replaces_the_stored_row(store):
    store.upsert_issues(issue_rows())
    newer = copy.deepcopy(fixture_nodes("issues", 1)[0])
    newer["title"] = "Retitled"
    newer["updatedAt"] = "2026-10-28T12:00:00Z"
    newer["labels"]["nodes"] = [{"name": "bug"}]

    store.upsert_issues([issue_row(newer)])

    title, labels, updated = store.con.execute(
        "SELECT title, labels, updated_at FROM issues WHERE number = ?", [newer["number"]]
    ).fetchone()
    assert (title, labels) == ("Retitled", ["bug"])
    assert updated == datetime(2026, 10, 28, 12, 0)  # stored as naive UTC
    assert store.count("issues") == 8


def test_an_older_snapshot_never_overwrites_a_newer_one(store):
    store.upsert_issues(issue_rows())
    stale = copy.deepcopy(fixture_nodes("issues", 1)[0])
    stale["title"] = "Stale"
    stale["updatedAt"] = "2020-01-01T00:00:00Z"

    store.upsert_issues([issue_row(stale)])

    (title,) = store.con.execute(
        "SELECT title FROM issues WHERE number = ?", [stale["number"]]
    ).fetchone()
    assert title != "Stale"


def test_duplicate_numbers_in_one_batch_keep_the_newest(store):
    node = fixture_nodes("issues", 1)[0]
    older = copy.deepcopy(node) | {"title": "older", "updatedAt": "2026-10-01T00:00:00Z"}
    newer = copy.deepcopy(node) | {"title": "newer", "updatedAt": "2026-10-02T00:00:00Z"}

    written = store.upsert_issues([issue_row(newer), issue_row(older)])

    assert written == 1
    (title,) = store.con.execute("SELECT title FROM issues").fetchone()
    assert title == "newer"


def test_pull_request_columns_round_trip(store):
    rows = pr_rows()
    store.upsert_pull_requests(rows)

    first_commit_at, labels, raw, author_type = store.con.execute(
        "SELECT first_commit_at, labels, raw, author_type FROM pull_requests WHERE number = 514"
    ).fetchone()
    assert first_commit_at == datetime(2026, 2, 9, 10, 41, 20)
    assert labels == []
    assert json.loads(raw)["number"] == 514
    assert author_type == "User"


def test_data_survives_reopening_the_file(tmp_path):
    path = db_path_for(tmp_path / "data", "pallets", "markupsafe")
    with Store(path) as store:
        store.upsert_issues(issue_rows())

    with Store(path) as reopened:
        assert reopened.count("issues") == 8
    assert path.name == "pallets__markupsafe.duckdb"


def test_last_successful_run_skips_failed_and_unfinished_runs(store):
    def run(status: str | None, watermark: datetime) -> int:
        run_id = store.start_run(
            "pallets/markupsafe",
            mode="full",
            since_requested=None,
            coverage_from=None,
            issues_since=None,
            prs_since=None,
        )
        if status:
            store.finish_run(
                run_id,
                status=status,
                issues_fetched=1,
                prs_fetched=1,
                issues_watermark=watermark,
                prs_watermark=watermark,
                api_calls=2,
                api_cost=2,
            )
        return run_id

    assert store.last_successful_run("pallets/markupsafe") is None
    good = run("success", utc(2026, 10, 1))
    run("failed", utc(2026, 10, 2))
    run(None, utc(2026, 10, 3))  # still "running", e.g. the process was killed

    previous = store.last_successful_run("pallets/markupsafe")
    assert previous.run_id == good
    assert previous.issues_watermark == utc(2026, 10, 1)
    assert previous.coverage_from is None
    assert store.last_successful_run("someone/else") is None


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="UTC"):
        _to_db(datetime(2026, 1, 1))
