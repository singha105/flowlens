from datetime import UTC, datetime

from fakes import load_fixture
from flowlens.config import parse_config
from flowlens.ingest.delivery import (
    deployment_row,
    deployment_status_rows,
    release_row,
    workflow_run_row,
)
from flowlens.model.deploys import (
    deploys_from_deployments,
    deploys_from_releases,
    deploys_from_workflow_runs,
    resolve_deploys,
)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


RELEASES = [
    release_row(node)
    for node in load_fixture("releases_page1.json")["data"]["repository"]["releases"]["nodes"]
]
_DEPLOYMENT_NODES = load_fixture("deployments_page1.json")["data"]["repository"]["deployments"][
    "nodes"
]
DEPLOYMENTS = [deployment_row(node) for node in _DEPLOYMENT_NODES]
STATUSES = [row for node in _DEPLOYMENT_NODES for row in deployment_status_rows(node)]
RUNS = [
    workflow_run_row(run)
    for page in (1, 2)
    for run in load_fixture(f"workflow_runs_page{page}.json")["workflow_runs"]
]


# --- releases ---------------------------------------------------------------------------


def test_each_published_release_is_a_deploy_at_publish_time():
    deploys = deploys_from_releases(RELEASES, include_prereleases=False)

    assert [d.name for d in deploys] == ["1.66.1", "1.66.0", "1.65.0", "1.64.0", "1.63.0"]
    assert deploys[0].deployed_at == utc(2026, 10, 21, 17, 3, 6)
    assert deploys[0].sha == "ca6718b7d3d788c31c97d1bc9144fd8388168f57"
    assert {d.status for d in deploys} == {"success"}


def test_drafts_never_count_and_prereleases_only_when_asked():
    draft = RELEASES[0] | {"id": "draft", "is_draft": True, "published_at": None}
    rc = RELEASES[0] | {"id": "rc", "tag_name": "1.67.0rc1", "is_prerelease": True}

    assert deploys_from_releases([draft, rc], include_prereleases=False) == []
    assert [d.name for d in deploys_from_releases([draft, rc], include_prereleases=True)] == [
        "1.67.0rc1"
    ]


# --- deployments ------------------------------------------------------------------------


def test_deployments_to_other_environments_are_ignored():
    assert deploys_from_deployments(DEPLOYMENTS, STATUSES, environments=("production",)) == []


def test_recorded_deployments_succeed_at_their_first_success_status():
    deploys = deploys_from_deployments(DEPLOYMENTS, STATUSES, environments=("Nightly",))

    assert len(deploys) == 37
    failed = [d for d in deploys if d.status == "failure"]
    assert len(failed) == 1
    assert failed[0].deployed_at == utc(2026, 10, 29, 4, 54, 54)
    assert deploys[1].deployed_at == utc(2026, 10, 28, 4, 56, 42)
    assert deploys[1].environment == "nightly"


def deployment(id_: str, latest: str | None = None) -> dict:
    return {
        "id": id_,
        "environment": "production",
        "ref_name": "main",
        "sha": "abc",
        "created_at": utc(2026, 1, 1, 9),
        "updated_at": utc(2026, 1, 1, 10),
        "latest_state": latest,
    }


def status(deployment_id: str, state: str, minute: int) -> dict:
    return {
        "id": f"{deployment_id}-{minute}",
        "deployment_id": deployment_id,
        "state": state,
        "created_at": utc(2026, 1, 1, 9, minute),
    }


def test_a_retried_deployment_succeeds_when_it_first_succeeded():
    deploys = deploys_from_deployments(
        [deployment("d")],
        [status("d", "FAILURE", 5), status("d", "SUCCESS", 20), status("d", "INACTIVE", 50)],
        environments=("production",),
    )

    assert [(d.status, d.deployed_at) for d in deploys] == [("success", utc(2026, 1, 1, 9, 20))]


def test_unfinished_deployments_are_skipped():
    deploys = deploys_from_deployments(
        [deployment("d", "IN_PROGRESS")],
        [status("d", "QUEUED", 1), status("d", "IN_PROGRESS", 2)],
        environments=("production",),
    )

    assert deploys == []


def test_latest_state_stands_in_when_statuses_were_not_fetched():
    deploys = deploys_from_deployments(
        [deployment("d", "SUCCESS")], [], environments=("production",)
    )

    assert [(d.status, d.deployed_at) for d in deploys] == [("success", utc(2026, 1, 1, 10))]


# --- workflow runs ----------------------------------------------------------------------


def test_completed_runs_are_deploys_and_cancelled_ones_are_not():
    deploys = deploys_from_workflow_runs(RUNS, workflow="Build Release")

    assert [d.status for d in deploys] == ["success", "failure", "failure", "success", "success"]
    assert deploys[0].deployed_at == utc(2026, 10, 21, 17, 3, 10)  # when the run finished
    assert deploys[0].source == "workflow:Build Release"


def test_runs_match_the_workflow_by_file_name_too():
    assert len(deploys_from_workflow_runs(RUNS, workflow="release.yml")) == 5
    assert deploys_from_workflow_runs(RUNS, workflow="ci.yml") == []


def test_in_progress_runs_are_not_deploys_yet():
    running = RUNS[0] | {"id": 1, "status": "in_progress", "conclusion": None}

    assert deploys_from_workflow_runs([running], workflow="Build Release") == []


# --- choosing the source ----------------------------------------------------------------


def resolve(source: str, **settings):
    config = parse_config({"deployment_source": source, **settings})
    return resolve_deploys(
        config, releases=RELEASES, deployments=DEPLOYMENTS, statuses=STATUSES, runs=RUNS
    )


def test_the_configured_source_decides_where_deploys_come_from():
    assert {d.source for d in resolve("releases")} == {"releases"}
    assert len(resolve("releases")) == 5
    assert len(resolve("deployments", deployment_environments=["release"])) == 3
    assert len(resolve("workflow:Build Release")) == 5
