import copy
from datetime import UTC, datetime

import httpx
import pytest

from fakes import FakeGitHub, FakeRepo, fixture_nodes, load_fixture
from flowlens.ingest.client import GitHubAuthError, GitHubError
from flowlens.ingest.github import format_ts
from flowlens.ingest.runner import (
    FETCH_VERSION,
    REFRESH_WINDOW,
    WATERMARK_OVERLAP,
    ingest_repo,
    parse_repo,
    plan_run,
)
from flowlens.storage import PreviousRun, Store, db_path_for


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


OCT_1 = utc(2025, 10, 1)
WATERMARK = utc(2026, 10, 20, 12, 0)
LOOKBACKS = {"issues": WATERMARK_OVERLAP, "releases": REFRESH_WINDOW}
NEWEST_ISSUE = utc(2026, 10, 18, 0, 9, 49)


def previous(coverage_from=None, fetch_version=FETCH_VERSION, **watermarks) -> PreviousRun:
    return PreviousRun(
        run_id=1,
        fetch_version=fetch_version,
        coverage_from=coverage_from,
        finished_at=WATERMARK,
        watermarks=watermarks or {"issues": WATERMARK, "releases": WATERMARK},
    )


def plan(prev, *, since=OCT_1, full=False):
    return plan_run(prev, since=since, full=full, lookbacks=LOOKBACKS)


# --- planning ---------------------------------------------------------------------------


def test_first_run_is_a_full_fetch_from_since():
    result = plan(None)

    assert (result.mode, result.coverage_from) == ("full", OCT_1)
    assert result.since == {"issues": OCT_1, "releases": OCT_1}


def test_rerun_resumes_each_entity_from_its_own_watermark():
    result = plan(previous(coverage_from=OCT_1))

    assert result.mode == "incremental"
    assert result.since == {
        "issues": WATERMARK - WATERMARK_OVERLAP,
        "releases": WATERMARK - REFRESH_WINDOW,  # recent releases are re-read for changes
    }
    assert result.coverage_from == OCT_1


def test_a_database_with_all_history_covers_any_since():
    assert plan(previous(coverage_from=None)).mode == "incremental"
    assert plan(previous(coverage_from=None), since=None).mode == "incremental"


@pytest.mark.parametrize("since", [utc(2025, 1, 1), None])
def test_asking_for_older_history_than_stored_triggers_a_full_fetch(since):
    result = plan(previous(coverage_from=OCT_1), since=since)

    assert result.mode == "full"
    assert result.since["issues"] == since
    assert result.coverage_from == since


def test_full_flag_ignores_the_previous_run():
    assert plan(previous(coverage_from=None), full=True).mode == "full"


@pytest.mark.parametrize("version", [None, FETCH_VERSION - 1])
def test_data_from_older_queries_is_refetched_in_full(version):
    assert plan(previous(coverage_from=OCT_1, fetch_version=version)).mode == "full"


def test_a_new_entity_starts_from_the_coverage_floor():
    # e.g. a workflow added to flowlens.yaml after the first run
    result = plan(previous(coverage_from=OCT_1, issues=WATERMARK))

    assert result.since["releases"] == OCT_1


def test_resume_point_never_drops_below_the_floor():
    floor = WATERMARK - WATERMARK_OVERLAP / 2
    result = plan(previous(coverage_from=floor), since=floor)

    assert result.since["issues"] == floor
    assert result.since["releases"] == floor


@pytest.mark.parametrize("bad", ["markupsafe", "/markupsafe", "pallets/", "a/b/c", ""])
def test_parse_repo_rejects_anything_but_owner_slash_name(bad):
    with pytest.raises(ValueError):
        parse_repo(bad)


def test_parse_repo_accepts_owner_slash_name():
    assert parse_repo(" pallets/markupsafe ") == ("pallets", "markupsafe")


# --- end to end against a fake GitHub ---------------------------------------------------


def delivery() -> dict:
    return {
        "releases": load_fixture("releases_page1.json")["data"]["repository"]["releases"]["nodes"],
        "deployments": load_fixture("deployments_page1.json")["data"]["repository"]["deployments"][
            "nodes"
        ],
        "workflows": load_fixture("workflows.json")["workflows"],
        "runs": [
            run
            for page in (1, 2)
            for run in load_fixture(f"workflow_runs_page{page}.json")["workflow_runs"]
        ],
    }


@pytest.fixture
def repo():
    return FakeRepo(fixture_nodes("issues", 3), fixture_nodes("pull_requests", 2), **delivery())


def ingest(repo, tmp_path, **kwargs):
    fake = FakeGitHub(repo, rest=repo.rest)
    summary = ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=fake.client(), **kwargs)
    return summary, fake


def entity_rows(data_dir) -> list[tuple]:
    with Store(db_path_for(data_dir, "pallets", "markupsafe")) as store:
        return store.con.execute(
            "SELECT r.run_id, r.mode, r.status, e.entity, e.fetched, e.watermark "
            "FROM ingest_runs r JOIN ingest_entities e USING (run_id) ORDER BY r.run_id, e.entity"
        ).fetchall()


def test_first_run_fetches_everything_and_reports_usage(repo, tmp_path):
    summary, _ = ingest(repo, tmp_path)

    assert summary.mode == "full"
    assert summary.fetched == {"issues": 8, "pull_requests": 8, "releases": 5, "deployments": 40}
    assert summary.stored == {
        "issues": 8,
        "pull_requests": 8,
        "releases": 5,
        "deployments": 40,
        "workflow_runs": 0,
    }
    assert summary.api_calls == 4  # one page per entity at the default page sizes
    assert summary.rate_remaining == 4000
    assert summary.first_updated == utc(2026, 10, 2, 13, 10, 54)
    assert summary.last_updated == utc(2026, 10, 21, 0, 9, 6)
    assert summary.db_path == tmp_path / "pallets__markupsafe.duckdb"


def test_workflow_runs_are_fetched_for_named_workflows(repo, tmp_path):
    summary, fake = ingest(repo, tmp_path, workflows=("Build Release", "release.yml"))

    # Both names resolve to the same workflow, which is fetched once.
    assert summary.fetched["workflow_runs:Build Release"] == 6
    assert len(summary.fetched) == 5
    assert summary.stored["workflow_runs"] == 6
    runs_path = "/repos/pallets/markupsafe/actions/workflows/34730317/runs"
    assert [path for path, _ in fake.rest_requests].count(runs_path) == 1


def test_an_unknown_workflow_lists_the_available_ones(repo, tmp_path):
    with pytest.raises(GitHubError, match=r"No workflow 'deploy\.yml'.*Build Release"):
        ingest(repo, tmp_path, workflows=("deploy.yml",))


def test_rerun_only_fetches_what_changed(repo, tmp_path):
    ingest(repo, tmp_path)

    second, fake = ingest(repo, tmp_path)

    assert second.mode == "incremental"
    # Only the newest issue and PR fall inside the five-minute overlap window...
    assert (second.fetched["issues"], second.fetched["pull_requests"]) == (1, 1)
    assert fake.requests[0]["variables"]["since"] == format_ts(NEWEST_ISSUE - WATERMARK_OVERLAP)
    # ...while releases and deployments from the last three days are read again.
    assert second.fetched["releases"] == 1
    newest_deployment = utc(2026, 10, 29, 4, 51, 33)
    recent = [
        d
        for d in repo.deployments
        if datetime.fromisoformat(d["createdAt"]) >= newest_deployment - REFRESH_WINDOW
    ]
    assert second.fetched["deployments"] == len(recent)
    assert second.stored["issues"] == 8

    # Someone edits an older PR; the next run picks up exactly that change.
    edited = next(n for n in repo.pull_requests if n["number"] == 510)
    edited.update(title="Edited title", updatedAt="2026-10-28T09:00:00Z")
    third, _ = ingest(repo, tmp_path)

    assert third.fetched["pull_requests"] == 2  # the edited PR, plus #519 from the overlap
    with Store(db_path_for(tmp_path, "pallets", "markupsafe")) as store:
        (title,) = store.con.execute(
            "SELECT title FROM pull_requests WHERE number = 510"
        ).fetchone()
    assert title == "Edited title"


def test_a_workflow_added_later_is_fetched_over_all_stored_history(repo, tmp_path):
    ingest(repo, tmp_path)

    summary, _ = ingest(repo, tmp_path, workflows=("Build Release",))

    assert summary.mode == "incremental"
    assert summary.since["workflow_runs:Build Release"] is None  # all history
    assert summary.fetched["workflow_runs:Build Release"] == 6


def test_rerun_with_nothing_new_keeps_the_watermarks(repo, tmp_path):
    ingest(repo, tmp_path)
    ingest(FakeRepo(workflows=repo.workflows), tmp_path)

    _, fake = ingest(repo, tmp_path)

    assert fake.requests[0]["variables"]["since"] == format_ts(NEWEST_ISSUE - WATERMARK_OVERLAP)
    watermarks = {entity: mark for run, _, _, entity, _, mark in entity_rows(tmp_path) if run == 2}
    assert watermarks["issues"] == NEWEST_ISSUE.replace(tzinfo=None)
    assert watermarks["releases"] == datetime(2026, 10, 21, 16, 19, 49)


def test_since_filters_client_side_streams(repo, tmp_path):
    summary, _ = ingest(repo, tmp_path, since=utc(2026, 10, 17, 0, 9, 18))

    assert summary.fetched["pull_requests"] == 6
    assert summary.fetched["issues"] == 1  # only #533 was updated after that moment
    assert summary.fetched["releases"] == 1  # 1.66.1, created on the 21st


def test_a_failed_run_is_recorded_and_does_not_move_the_watermark(repo, tmp_path):
    ingest(repo, tmp_path)
    updated = copy.deepcopy(repo.issues)
    for node in updated:
        node["updatedAt"] = "2026-10-28T10:00:00Z"

    def fail_on_pull_requests(body):
        if "pullRequests(" in body["query"]:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return FakeRepo(updated)(body)

    with pytest.raises(GitHubAuthError):
        ingest_repo(
            "pallets/markupsafe",
            data_dir=tmp_path,
            client=FakeGitHub(fail_on_pull_requests).client(),
        )

    failed = [row for row in entity_rows(tmp_path) if row[0] == 2]
    assert [(status, entity, fetched) for _, _, status, entity, fetched, _ in failed] == [
        ("failed", "issues", 8)  # what it did fetch was kept and recorded
    ]
    with Store(db_path_for(tmp_path, "pallets", "markupsafe")) as store:
        last_good = store.last_successful_run("pallets/markupsafe")
        (error,) = store.con.execute(
            "SELECT error FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert last_good.run_id == 1
    assert last_good.watermarks["issues"] == NEWEST_ISSUE
    assert "401" in error


def test_watermarks_track_creation_time_for_delivery_streams(repo, tmp_path):
    ingest(repo, tmp_path, workflows=("Build Release",))

    marks = {entity: mark for _, _, _, entity, _, mark in entity_rows(tmp_path)}

    assert marks["releases"] == datetime(2026, 10, 21, 16, 19, 49)
    assert marks["deployments"] == datetime(2026, 10, 29, 4, 51, 33)
    assert marks["workflow_runs:Build Release"] == datetime(2026, 10, 21, 16, 21, 2)
    assert set(marks) == {
        "issues",
        "pull_requests",
        "releases",
        "deployments",
        "workflow_runs:Build Release",
    }
