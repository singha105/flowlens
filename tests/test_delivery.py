from datetime import UTC, datetime, timedelta

import pytest

from fakes import NOW, FakeGitHub, FakeRepo, load_fixture, recorded_pages
from flowlens.ingest.client import GitHubError
from flowlens.ingest.delivery import (
    RUN_SEARCH_CAP,
    deployment_row,
    deployment_status_rows,
    fetch_deployments,
    fetch_releases,
    fetch_workflow_runs,
    find_workflow,
    release_row,
    workflow_run_row,
)
from flowlens.ingest.github import format_ts


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


RELEASES = load_fixture("releases_page1.json")["data"]["repository"]["releases"]["nodes"]
DEPLOYMENTS = load_fixture("deployments_page1.json")["data"]["repository"]["deployments"]["nodes"]
WORKFLOWS = load_fixture("workflows.json")["workflows"]
RUNS = [
    run
    for page in (1, 2)
    for run in load_fixture(f"workflow_runs_page{page}.json")["workflow_runs"]
]
BUILD_RELEASE = next(w for w in WORKFLOWS if w["name"] == "Build Release")


# --- releases ---------------------------------------------------------------------------


def test_release_row_keeps_tag_publish_time_and_target_commit():
    row = release_row(RELEASES[0])

    assert row["tag_name"] == "1.66.1"
    assert row["created_at"] == utc(2026, 10, 21, 16, 19, 49)
    assert row["published_at"] == utc(2026, 10, 21, 17, 3, 6)
    assert row["target_sha"] == "ca6718b7d3d788c31c97d1bc9144fd8388168f57"
    assert (row["is_draft"], row["is_prerelease"]) == (False, False)


def test_releases_stop_at_since():
    fake = FakeGitHub(recorded_pages("releases", 1))

    pages = list(fetch_releases(fake.client(), "streamlit", "streamlit", utc(2026, 10, 1)))

    assert [n["tagName"] for page in pages for n in page] == ["1.66.1", "1.66.0", "1.65.0"]
    assert len(fake.requests) == 1  # 1.64.0 is older, so page 2 is never requested


# --- deployments ------------------------------------------------------------------------


def test_deployment_row_and_its_status_history():
    failed = DEPLOYMENTS[0]

    row = deployment_row(failed)
    statuses = deployment_status_rows(failed)

    assert (row["environment"], row["ref_name"], row["latest_state"]) == (
        "nightly",
        "develop",
        "FAILURE",
    )
    assert row["sha"].startswith("6cd662a8")
    assert [s["state"] for s in statuses] == ["FAILURE", "IN_PROGRESS", "QUEUED", "WAITING"]
    assert {s["deployment_id"] for s in statuses} == {failed["id"]}


def test_deployments_cover_every_environment():
    fake = FakeGitHub(FakeRepo(deployments=DEPLOYMENTS))

    (page,) = fetch_deployments(fake.client(), "streamlit", "streamlit")

    assert {n["environment"] for n in page} == {"nightly", "release"}


# --- workflows and their runs -----------------------------------------------------------


@pytest.mark.parametrize(
    "wanted", ["Build Release", "build release", "release.yml", ".github/workflows/release.yml"]
)
def test_find_workflow_by_name_or_file(wanted):
    fake = FakeGitHub(rest=FakeRepo(workflows=WORKFLOWS).rest)

    assert find_workflow(fake.client(), "streamlit", "streamlit", wanted)["id"] == 34730317


def test_find_workflow_lists_alternatives_when_missing():
    fake = FakeGitHub(rest=FakeRepo(workflows=WORKFLOWS).rest)

    with pytest.raises(GitHubError, match=r"No workflow 'deploy'.*Release Start"):
        find_workflow(fake.client(), "streamlit", "streamlit", "deploy")


def test_workflow_runs_page_with_link_headers():
    fake = FakeGitHub(rest=FakeRepo(workflows=WORKFLOWS, runs=RUNS).rest)

    pages = list(
        fetch_workflow_runs(
            fake.client(), "streamlit", "streamlit", BUILD_RELEASE, utc(2026, 10, 1), page_size=4
        )
    )

    assert [len(page) for page in pages] == [4, 2]
    assert [path for path, _ in fake.rest_requests] == [
        "/repos/streamlit/streamlit/actions/workflows/34730317/runs"
    ] * 2
    assert fake.rest_requests[0][1]["created"] == "2026-10-01T00:00:00Z..2026-10-29T05:00:00Z"


def test_workflow_runs_default_to_the_workflows_whole_life():
    fake = FakeGitHub(rest=FakeRepo(workflows=WORKFLOWS, runs=RUNS).rest)

    list(fetch_workflow_runs(fake.client(), "streamlit", "streamlit", BUILD_RELEASE))

    assert fake.rest_requests[0][1]["created"].startswith("2022-09-13T07:58:21Z..")


def test_searches_over_the_1000_result_cap_are_split_in_half():
    # A fake workflow with a run every 30 minutes for 40 days: 1,920 runs, over the cap.
    start = NOW - timedelta(days=40)
    middle = NOW - timedelta(days=20)
    many = [
        {"id": i, "workflow_id": 1, "created_at": format_ts(start + timedelta(minutes=30 * i))}
        for i in range(1920)
    ]
    assert len(many) > RUN_SEARCH_CAP
    fake = FakeGitHub(rest=FakeRepo(runs=many).rest)

    pages = fetch_workflow_runs(fake.client(), "o", "r", {"id": 1}, start, page_size=100)
    ids = [run["id"] for page in pages for run in page]

    first_pages = [params["created"] for _, params in fake.rest_requests if "page" not in params]
    assert first_pages == [
        f"{format_ts(start)}..{format_ts(NOW)}",  # 1,920 found: too many, so split
        f"{format_ts(middle)}..{format_ts(NOW)}",  # newer half first, 960 runs
        f"{format_ts(start)}..{format_ts(middle)}",
    ]
    assert len(fake.rest_requests) == 1 + 10 + 10
    assert set(ids) == set(range(1920))  # nothing lost to the cap
    assert len(ids) == 1921  # the run exactly at the midpoint, in both halves (upserts dedupe)


def test_workflow_run_row():
    row = workflow_run_row(RUNS[0])

    assert row["workflow_name"] == "Build Release"
    assert row["workflow_path"] == ".github/workflows/release.yml"
    assert (row["status"], row["conclusion"]) == ("completed", "success")
    assert row["head_sha"].startswith("ca6718b7")
    assert row["head_branch"] == "1.66.1"
    assert row["created_at"] == utc(2026, 10, 21, 16, 21, 2)
    assert row["updated_at"] == utc(2026, 10, 21, 17, 3, 10)
    assert row["url"].startswith("https://github.com/streamlit/streamlit/actions/runs/")


def test_release_builds_ship_the_released_commit():
    # The recorded data lines up: each successful Build Release run built its release's tag.
    released = {r["tag_name"]: r["target_sha"] for r in map(release_row, RELEASES)}
    built = [r for r in map(workflow_run_row, RUNS) if r["conclusion"] == "success"]

    assert len(built) == 3
    assert all(released[run["head_branch"]] == run["head_sha"] for run in built)
