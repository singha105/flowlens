import copy
from datetime import UTC, datetime

import httpx
import pytest

from fakes import FakeGitHub, FakeRepo, fixture_nodes
from flowlens.ingest.github import GitHubAuthError, format_ts
from flowlens.ingest.runner import WATERMARK_OVERLAP, ingest_repo, parse_repo, plan_run
from flowlens.storage import PreviousRun, Store, db_path_for


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


OCT_1 = utc(2025, 10, 1)
WATERMARK = utc(2026, 10, 20, 12, 0)


def previous(coverage_from=None, issues=WATERMARK, prs=WATERMARK) -> PreviousRun:
    return PreviousRun(
        run_id=1, coverage_from=coverage_from, issues_watermark=issues, prs_watermark=prs
    )


# --- planning ---------------------------------------------------------------------------


def test_first_run_is_a_full_fetch_from_since():
    plan = plan_run(None, since=OCT_1, full=False)

    assert (plan.mode, plan.issues_since, plan.prs_since, plan.coverage_from) == (
        "full",
        OCT_1,
        OCT_1,
        OCT_1,
    )


def test_rerun_resumes_from_the_watermark_minus_the_overlap():
    plan = plan_run(previous(coverage_from=OCT_1), since=OCT_1, full=False)

    assert plan.mode == "incremental"
    assert plan.issues_since == WATERMARK - WATERMARK_OVERLAP
    assert plan.prs_since == WATERMARK - WATERMARK_OVERLAP
    assert plan.coverage_from == OCT_1


def test_a_database_with_all_history_covers_any_since():
    assert plan_run(previous(coverage_from=None), since=OCT_1, full=False).mode == "incremental"
    assert plan_run(previous(coverage_from=None), since=None, full=False).mode == "incremental"


@pytest.mark.parametrize("since", [utc(2025, 1, 1), None])
def test_asking_for_older_history_than_stored_triggers_a_full_fetch(since):
    plan = plan_run(previous(coverage_from=OCT_1), since=since, full=False)

    assert plan.mode == "full"
    assert plan.issues_since == since
    assert plan.coverage_from == since


def test_full_flag_ignores_the_previous_run():
    assert plan_run(previous(coverage_from=None), since=OCT_1, full=True).mode == "full"


def test_missing_watermark_falls_back_to_the_coverage_floor():
    plan = plan_run(previous(coverage_from=OCT_1, issues=None), since=OCT_1, full=False)

    assert plan.issues_since == OCT_1


def test_resume_point_never_drops_below_the_floor():
    floor = WATERMARK - WATERMARK_OVERLAP / 2
    plan = plan_run(previous(coverage_from=floor), since=floor, full=False)

    assert plan.issues_since == floor


@pytest.mark.parametrize("bad", ["markupsafe", "/markupsafe", "pallets/", "a/b/c", ""])
def test_parse_repo_rejects_anything_but_owner_slash_name(bad):
    with pytest.raises(ValueError):
        parse_repo(bad)


def test_parse_repo_accepts_owner_slash_name():
    assert parse_repo(" pallets/markupsafe ") == ("pallets", "markupsafe")


# --- end to end against a fake GitHub ---------------------------------------------------


@pytest.fixture
def repo():
    return FakeRepo(fixture_nodes("issues", 3), fixture_nodes("pull_requests", 2))


def runs(data_dir) -> list[tuple]:
    with Store(db_path_for(data_dir, "pallets", "markupsafe")) as store:
        return store.con.execute(
            "SELECT mode, status, issues_fetched, prs_fetched, issues_since, prs_since "
            "FROM ingest_runs ORDER BY run_id"
        ).fetchall()


def test_first_run_fetches_everything_and_reports_usage(repo, tmp_path):
    fake = FakeGitHub(repo)

    summary = ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=fake.client())

    assert summary.mode == "full"
    assert (summary.issues_fetched, summary.prs_fetched) == (8, 8)
    assert (summary.issues_stored, summary.prs_stored) == (8, 8)
    assert summary.api_calls == 2  # one page each at the default page sizes
    assert summary.api_cost == 2
    assert summary.rate_remaining == 4000
    assert summary.first_updated == utc(2026, 10, 2, 13, 10, 54)
    assert summary.last_updated == utc(2026, 10, 21, 0, 9, 6)
    assert summary.db_path == tmp_path / "pallets__markupsafe.duckdb"


def test_rerun_only_fetches_what_changed(repo, tmp_path):
    ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=FakeGitHub(repo).client())

    fake = FakeGitHub(repo)
    second = ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=fake.client())

    assert second.mode == "incremental"
    # Only the newest issue and PR fall inside the five-minute overlap window.
    assert (second.issues_fetched, second.prs_fetched) == (1, 1)
    assert (second.issues_stored, second.prs_stored) == (8, 8)
    newest_issue = utc(2026, 10, 18, 0, 9, 49)
    assert fake.requests[0]["variables"]["since"] == format_ts(newest_issue - WATERMARK_OVERLAP)

    # Someone edits an older PR; the next run picks up exactly that change.
    edited = next(n for n in repo.pull_requests if n["number"] == 510)
    edited.update(title="Edited title", updatedAt="2026-10-28T09:00:00Z")
    third = ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=FakeGitHub(repo).client())

    assert third.prs_fetched == 2  # the edited PR, plus #519 from the overlap window
    with Store(db_path_for(tmp_path, "pallets", "markupsafe")) as store:
        (title,) = store.con.execute(
            "SELECT title FROM pull_requests WHERE number = 510"
        ).fetchone()
    assert title == "Edited title"
    assert [(mode, status) for mode, status, *_ in runs(tmp_path)] == [
        ("full", "success"),
        ("incremental", "success"),
        ("incremental", "success"),
    ]


def test_rerun_with_nothing_new_keeps_the_watermark(repo, tmp_path):
    empty = FakeRepo([], [])
    ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=FakeGitHub(repo).client())
    ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=FakeGitHub(empty).client())

    fake = FakeGitHub(repo)
    ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=fake.client())

    newest_issue = utc(2026, 10, 18, 0, 9, 49)
    assert fake.requests[0]["variables"]["since"] == format_ts(newest_issue - WATERMARK_OVERLAP)


def test_since_filters_pull_requests_client_side(repo, tmp_path):
    summary = ingest_repo(
        "pallets/markupsafe",
        since=utc(2026, 10, 17, 0, 9, 18),
        data_dir=tmp_path,
        client=FakeGitHub(repo).client(),
    )

    assert summary.prs_fetched == 6
    assert summary.issues_fetched == 1  # only #533 was updated after that moment


def test_a_failed_run_is_recorded_and_does_not_move_the_watermark(repo, tmp_path):
    ingest_repo("pallets/markupsafe", data_dir=tmp_path, client=FakeGitHub(repo).client())
    updated = copy.deepcopy(repo.issues)
    for node in updated:
        node["updatedAt"] = "2026-10-28T10:00:00Z"

    def fail_on_pull_requests(body):
        if "pullRequests(" in body["query"]:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return FakeRepo(updated, [])(body)

    with pytest.raises(GitHubAuthError):
        ingest_repo(
            "pallets/markupsafe",
            data_dir=tmp_path,
            client=FakeGitHub(fail_on_pull_requests).client(),
        )

    _mode, status, issues_fetched, *_ = runs(tmp_path)[-1]
    assert (status, issues_fetched) == ("failed", 8)  # issues it did fetch were kept
    with Store(db_path_for(tmp_path, "pallets", "markupsafe")) as store:
        last_good = store.last_successful_run("pallets/markupsafe")
        (error,) = store.con.execute(
            "SELECT error FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert last_good.run_id == 1
    assert last_good.issues_watermark == utc(2026, 10, 18, 0, 9, 49)
    assert "401" in error
