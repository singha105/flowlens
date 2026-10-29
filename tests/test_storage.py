import copy
import json
from datetime import UTC, datetime

import duckdb
import pytest

from fakes import FIXTURES, fixture_items, fixture_nodes, load_fixture
from flowlens.ingest.delivery import deployment_row, deployment_status_rows, workflow_run_row
from flowlens.ingest.github import issue_event_rows, issue_row, pull_request_row, review_rows
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
    return [
        pull_request_row(node, "pallets/markupsafe") for node in fixture_nodes("pull_requests", 2)
    ]


def snapshot(store: Store, table: str, order: str = "number") -> list[tuple]:
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
    return store.con.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}"
    ).fetchall()


# --- upserts ----------------------------------------------------------------------------


def test_upserting_the_same_rows_twice_changes_nothing(store):
    store.upsert("issues", issue_rows())
    store.upsert("pull_requests", pr_rows())
    issues_before = snapshot(store, "issues")
    prs_before = snapshot(store, "pull_requests")

    store.upsert("issues", issue_rows())
    store.upsert("pull_requests", pr_rows())

    assert store.count("issues") == 8
    assert store.count("pull_requests") == 8
    assert snapshot(store, "issues") == issues_before
    assert snapshot(store, "pull_requests") == prs_before


def test_a_newer_snapshot_replaces_the_stored_row(store):
    store.upsert("issues", issue_rows())
    newer = copy.deepcopy(fixture_nodes("issues", 1)[0])
    newer["title"] = "Retitled"
    newer["updatedAt"] = "2026-10-28T12:00:00Z"
    newer["labels"]["nodes"] = [{"name": "bug"}]

    store.upsert("issues", [issue_row(newer)])

    title, labels, updated = store.con.execute(
        "SELECT title, labels, updated_at FROM issues WHERE number = ?", [newer["number"]]
    ).fetchone()
    assert (title, labels) == ("Retitled", ["bug"])
    assert updated == datetime(2026, 10, 28, 12, 0)  # stored as naive UTC
    assert store.count("issues") == 8


def test_an_older_snapshot_never_overwrites_a_newer_one(store):
    store.upsert("issues", issue_rows())
    stale = copy.deepcopy(fixture_nodes("issues", 1)[0])
    stale["title"] = "Stale"
    stale["updatedAt"] = "2020-01-01T00:00:00Z"

    store.upsert("issues", [issue_row(stale)])

    (title,) = store.con.execute(
        "SELECT title FROM issues WHERE number = ?", [stale["number"]]
    ).fetchone()
    assert title != "Stale"


def test_duplicate_keys_in_one_batch_keep_the_newest(store):
    node = fixture_nodes("issues", 1)[0]
    older = copy.deepcopy(node) | {"title": "older", "updatedAt": "2026-10-01T00:00:00Z"}
    newer = copy.deepcopy(node) | {"title": "newer", "updatedAt": "2026-10-02T00:00:00Z"}

    written = store.upsert("issues", [issue_row(newer), issue_row(older)])

    assert written == 1
    (title,) = store.con.execute("SELECT title FROM issues").fetchone()
    assert title == "newer"


def test_pull_request_columns_round_trip(store):
    store.upsert(
        "pull_requests",
        [
            pull_request_row(n, "streamlit/streamlit")
            for n in fixture_items("streamlit_prs.json").values()
        ],
    )

    row = next(r for r in store.rows("pull_requests") if r["number"] == 17688)

    assert row["first_commit_at"] == utc(2026, 10, 27, 10, 0, 10)
    assert row["merge_commit_sha"] == "62bad13793731a5f65f5394a6b440f47ee671578"
    assert row["closing_issues"] == [17612]
    assert row["review_count"] == 0
    assert row["labels"] == ["change:bugfix", "impact:users"]
    (raw,) = store.con.execute("SELECT raw FROM pull_requests WHERE number = 17688").fetchone()
    assert json.loads(raw)["number"] == 17688


def test_events_reviews_and_delivery_rows_store_and_read_back(store):
    issues = fixture_items("streamlit_issues.json").values()
    prs = fixture_items("streamlit_prs.json").values()
    deployments = load_fixture("deployments_page1.json")["data"]["repository"]["deployments"]
    runs = load_fixture("workflow_runs_page1.json")["workflow_runs"]

    store.upsert("issue_events", [row for node in issues for row in issue_event_rows(node)])
    store.upsert("pr_reviews", [row for node in prs for row in review_rows(node)])
    store.upsert("deployments", [deployment_row(n) for n in deployments["nodes"]])
    store.upsert(
        "deployment_statuses",
        [row for n in deployments["nodes"] for row in deployment_status_rows(n)],
    )
    store.upsert("workflow_runs", [workflow_run_row(run) for run in runs])

    closed = next(
        e
        for e in store.rows("issue_events")
        if e["issue_number"] == 11752 and e["event_type"] == "closed"
    )
    assert (closed["pr_number"], closed["state_reason"]) == (16418, "COMPLETED")
    assert closed["created_at"].tzinfo == UTC
    assert store.count("pr_reviews") == 18
    assert store.count("deployments") == 40
    assert store.count("deployment_statuses") > 40
    assert {r["conclusion"] for r in store.rows("workflow_runs")} == {
        "success",
        "failure",
        "cancelled",
    }


def test_data_survives_reopening_the_file(tmp_path):
    path = db_path_for(tmp_path / "data", "pallets", "markupsafe")
    with Store(path) as store:
        store.upsert("issues", issue_rows())

    with Store(path) as reopened:
        assert reopened.count("issues") == 8
    assert path.name == "pallets__markupsafe.duckdb"


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="UTC"):
        _to_db(datetime(2026, 1, 1))


# --- model tables -----------------------------------------------------------------------


def work_item(number: int, title: str) -> dict:
    return {
        "id": f"issue#{number}", "type": "issue", "number": number, "title": title,
        "url": None, "author": None, "author_type": None, "created_at": utc(2026, 1, 1),
        "started_at": None, "started_signal": None, "done_at": None, "outcome": "open",
        "labels": [], "kinds": [], "linked": [], "reopen_count": 0, "excluded": False,
        "exclude_reason": None,
    }  # fmt: skip


def test_replace_swaps_model_tables_wholesale(store):
    store.replace({"work_items": [work_item(1, "one"), work_item(2, "two")]})
    store.replace({"work_items": [work_item(3, "three")]})

    assert [r["number"] for r in store.rows("work_items")] == [3]


def test_replace_rolls_back_on_error(store):
    store.replace({"work_items": [work_item(1, "one")]})
    broken = {
        "id": "R_1", "source": "releases", "name": "1.0", "sha": None, "environment": None,
        "status": "success", "deployed_at": datetime(2026, 1, 1), "url": None,  # naive
    }  # fmt: skip

    with pytest.raises(ValueError):
        store.replace({"work_items": [work_item(2, "two")], "deploys": [broken]})

    assert [r["number"] for r in store.rows("work_items")] == [1]


def test_replace_refuses_source_tables(store):
    with pytest.raises(ValueError, match="not a model table"):
        store.replace({"issues": []})


# --- runs and watermarks ----------------------------------------------------------------


def test_last_successful_run_skips_failed_and_unfinished_runs(store):
    def run(status: str | None, watermark: datetime) -> int:
        run_id = store.start_run(
            "pallets/markupsafe",
            mode="full",
            fetch_version=2,
            since_requested=None,
            coverage_from=None,
        )
        for entity in ("issues", "pull_requests"):
            store.record_entity(run_id, entity, since_used=None, fetched=1, watermark=watermark)
        if status:
            store.finish_run(run_id, status=status, api_calls=2, api_cost=2)
        return run_id

    assert store.last_successful_run("pallets/markupsafe") is None
    good = run("success", utc(2026, 10, 1))
    run("failed", utc(2026, 10, 2))
    run(None, utc(2026, 10, 3))  # still "running", e.g. the process was killed

    previous = store.last_successful_run("pallets/markupsafe")
    assert previous.run_id == good
    assert previous.fetch_version == 2
    assert previous.watermarks == {"issues": utc(2026, 10, 1), "pull_requests": utc(2026, 10, 1)}
    assert previous.coverage_from is None
    assert previous.finished_at is not None
    assert store.last_successful_run("someone/else") is None


def test_a_day_1_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute((FIXTURES / "day1_schema.sql").read_text())
    con.execute(
        "INSERT INTO ingest_runs (repo, mode, status, started_at, finished_at, coverage_from) "
        "VALUES ('pallets/markupsafe', 'full', 'success', now(), now(), NULL)"
    )
    con.close()

    with Store(path) as store:
        columns = {
            name
            for (name,) in store.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'pull_requests'"
            ).fetchall()
        }
        previous = store.last_successful_run("pallets/markupsafe")
        store.upsert("pull_requests", pr_rows())

    assert {"merge_commit_sha", "first_approval_at", "review_count", "closing_issues"} <= columns
    # No fetch version means the data predates today's queries; the runner refetches it.
    assert previous.fetch_version is None
    assert previous.watermarks == {}
