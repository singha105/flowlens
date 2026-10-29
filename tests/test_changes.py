"""Mapping merged PRs to the deploy that shipped them, and fetching the compare ranges."""

from datetime import UTC, datetime

import httpx
import pytest

from fakes import FakeGitHub, load_fixture
from flowlens.config import Config
from flowlens.ingest.client import API_URL
from flowlens.ingest.delivery import compare_commit_rows, compare_row, fetch_compare
from flowlens.ingest.runner import fetch_deploy_ranges
from flowlens.model.build import build_model
from flowlens.model.changes import (
    DeploymentChange,
    Range,
    deploy_ranges,
    map_changes,
    referenced_pr,
)
from flowlens.model.deploys import Deploy
from flowlens.storage import Store

V1660, V1661 = (
    "269e552f276f5757b2903174da18e083defb325b",
    "ca6718b7d3d788c31c97d1bc9144fd8388168f57",
)
COMPARE = load_fixture("compare_1.66.0...1.66.1.json")


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def deploy(name: str, day: int, sha: str | None, status: str = "success") -> Deploy:
    return Deploy(
        id=f"release:{name}",
        source="releases",
        name=name,
        sha=sha,
        environment=None,
        status=status,
        deployed_at=utc(2026, 10, day, 12),
        url=None,
    )


def pr(number: int, merge_sha: str | None, merged: bool = True) -> dict:
    return {
        "number": number,
        "merge_commit_sha": merge_sha,
        "merged_at": utc(2026, 10, 1) if merged else None,
    }


def commits(base: str, head: str, *messages_and_shas: tuple[str, str]) -> list[dict]:
    return [
        {
            "compare_id": f"{base}...{head}",
            "position": position,
            "sha": sha,
            "message": message,
        }
        for position, (message, sha) in enumerate(messages_and_shas)
    ]


# --- ranges -----------------------------------------------------------------------------


def test_each_successful_deploy_ships_the_commits_since_the_one_before():
    deploys = [
        deploy("v3", 20, "c3"),
        deploy("v1", 5, "c1"),
        deploy("v2-broken", 10, "cx", status="failure"),  # never shipped anything
        deploy("v2", 12, "c2"),
    ]

    assert deploy_ranges(deploys) == [
        Range("release:v1", None, "c1"),
        Range("release:v2", "c1", "c2"),
        Range("release:v3", "c2", "c3"),
    ]
    assert [r.compare_id for r in deploy_ranges(deploys)] == [None, "c1...c2", "c2...c3"]


def test_a_deploy_without_a_commit_gets_no_range_and_the_next_one_skips_it():
    ranges = deploy_ranges([deploy("v1", 5, "c1"), deploy("v2", 6, None), deploy("v3", 7, "c3")])

    assert ranges[1] == Range("release:v2", "c1", None)
    assert ranges[1].compare_id is None
    assert ranges[2] == Range("release:v3", "c1", "c3")


def test_a_redeploy_of_the_same_commit_has_nothing_to_compare():
    ranges = deploy_ranges([deploy("v1", 5, "c1"), deploy("v1-again", 6, "c1")])

    assert ranges[1].compare_id is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[fix] Use canonical timezone names (#17567)\n\nBody (#99)", 17567),
        ("Merge pull request #42 from someone/branch", 42),
        ("Up version to 1.66.1", None),
        ('Revert "Add the thing (#120)"', None),  # a direct revert names what it undoes
        ("fix #9810 Update to latest Material symbols font (#9813)", 9813),
    ],
)
def test_referenced_pr_reads_squash_and_merge_subjects(message, expected):
    assert referenced_pr(message) == expected


# --- mapping ----------------------------------------------------------------------------


def test_prs_are_matched_by_merge_commit_or_by_the_number_in_the_subject():
    deploys = [deploy("v1", 5, "c1"), deploy("v2", 12, "c2")]
    rows = commits(
        "c1", "c2",
        ("Add a feature (#10)", "m10"),          # squash merge: its merge commit
        ("Cherry-picked fix (#11)", "pick11"),   # a different SHA that names #11
        ("Up version to 2", "bump"),             # no PR
        ("Mentions an issue (#12)", "m-issue"),  # #12 isn't a merged PR
    )  # fmt: skip
    prs = [pr(10, "m10"), pr(11, "m11"), pr(12, None, merged=False)]

    assert map_changes(deploys, rows, prs) == [
        DeploymentChange(10, "release:v2", "m10", "merge_commit"),
        DeploymentChange(11, "release:v2", "pick11", "pr_reference"),
    ]


def test_a_cherry_pick_ships_the_pr_before_the_release_with_its_merge_commit():
    deploys = [deploy("v1.0", 1, "a"), deploy("v1.0.1", 5, "b"), deploy("v1.1", 12, "c")]
    rows = [
        *commits("a", "b", ("Fix a crash (#20)", "pick20")),
        *commits("b", "c", ("Fix a crash (#20)", "m20"), ("New thing (#21)", "m21")),
    ]

    changes = map_changes(deploys, rows, [pr(20, "m20"), pr(21, "m21")])

    assert {c.pr_number: c.deploy_id for c in changes} == {
        20: "release:v1.0.1",
        21: "release:v1.1",
    }


def test_the_first_deploy_ships_nothing_known():
    rows = commits("zero", "c1", ("Early work (#1)", "m1"))

    assert map_changes([deploy("v1", 5, "c1")], rows, [pr(1, "m1")]) == []


# --- fetching ---------------------------------------------------------------------------


def compare_pages(per_page: int = 2):
    """Serve the recorded 1.66.0...1.66.1 comparison in pages of ``per_page`` commits."""
    payload = COMPARE

    def rest(path: str, params: dict[str, str]) -> httpx.Response:
        assert path == f"/repos/streamlit/streamlit/compare/{V1660}...{V1661}", path
        page, size = int(params.get("page", 1)), int(params["per_page"])
        size = min(size, per_page)
        chunk = payload["commits"][(page - 1) * size : page * size]
        headers = {}
        if page * size < len(payload["commits"]):
            next_url = f"{API_URL}{path}?per_page={params['per_page']}&page={page + 1}"
            headers["Link"] = f'<{next_url}>; rel="next"'
        body = {**payload, "commits": chunk, "files": payload["files"] if page == 1 else []}
        return httpx.Response(200, json=body, headers=headers)

    return rest


def test_fetch_compare_joins_every_page_oldest_first():
    fake = FakeGitHub(rest=compare_pages(per_page=3))

    summary, fetched = fetch_compare(fake.client(), "streamlit", "streamlit", V1660, V1661)

    assert len(fake.rest_requests) == 2
    assert summary["total_commits"] == 4 and summary["status"] == "ahead"
    assert [c["sha"][:7] for c in fetched] == ["c706d9a", "7526d1b", "1359d6e", "ca6718b"]


def test_compare_rows_keep_messages_authors_and_totals():
    _, fetched = fetch_compare(
        FakeGitHub(rest=compare_pages()).client(), "streamlit", "streamlit", V1660, V1661
    )
    rows = compare_commit_rows(V1660, V1661, fetched)
    row = compare_row(V1660, V1661, COMPARE, fetched, utc(2026, 10, 29))

    assert row["id"] == f"{V1660}...{V1661}"
    assert (row["status"], row["total_commits"], row["fetched"]) == ("ahead", 4, 4)
    assert rows[1]["message"] == "[chore] Allow Pillow 13 (#17589)"
    assert rows[1]["author"] == "lukasmasuch"
    assert rows[1]["authored_at"] == utc(2026, 10, 21, 10, 17, 53)
    assert [r["position"] for r in rows] == [0, 1, 2, 3]


def releases_store(tmp_path, *shas: str) -> Store:
    store = Store(tmp_path / "repo.duckdb")
    store.upsert(
        "releases",
        [
            {
                "id": f"R{i}",
                "tag_name": f"1.66.{i}",
                "name": None,
                "url": None,
                "is_draft": False,
                "is_prerelease": False,
                "created_at": utc(2026, 10, 10 + i),
                "published_at": utc(2026, 10, 10 + i),
                "target_sha": sha,
                "author": None,
                "raw": {},
            }
            for i, sha in enumerate(shas)
        ],
    )
    return store


def test_ingest_fetches_each_missing_range_once_and_keeps_it(tmp_path):
    fake = FakeGitHub(rest=compare_pages())
    with releases_store(tmp_path, V1660, V1661) as store:
        first = fetch_deploy_ranges(
            fake.client(), store, "streamlit", "streamlit", Config(), lambda _: None
        )
        calls = len(fake.rest_requests)
        again = fetch_deploy_ranges(
            fake.client(), store, "streamlit", "streamlit", Config(), lambda _: None
        )
        stored = store.rows("compares")

    assert (first.deploys, first.needed, first.fetched, first.commits) == (2, 1, 1, 4)
    assert calls == 2  # 4 commits in pages of 2
    assert (again.fetched, len(fake.rest_requests)) == (0, calls)
    assert [row["fetched"] for row in stored] == [4]


def test_a_commit_github_no_longer_has_is_recorded_not_retried(tmp_path):
    def gone(path, _params):
        return httpx.Response(404, json={"message": "Not Found"})

    fake = FakeGitHub(rest=gone)
    with releases_store(tmp_path, "aaa", "bbb") as store:
        tally = fetch_deploy_ranges(fake.client(), store, "o", "r", Config(), lambda _: None)
        retry = fetch_deploy_ranges(fake.client(), store, "o", "r", Config(), lambda _: None)
        (row,) = store.rows("compares")

    assert (tally.fetched, tally.not_found, retry.fetched) == (1, 1, 0)
    assert (row["status"], row["fetched"]) == ("not_found", 0)


def test_the_model_stores_which_deploy_shipped_each_pr(tmp_path):
    with releases_store(tmp_path, V1660, V1661) as store:
        fetch_deploy_ranges(
            FakeGitHub(rest=compare_pages()).client(),
            store,
            "streamlit",
            "streamlit",
            Config(),
            lambda _: None,
        )
        store.upsert(
            "pull_requests",
            [
                {
                    "number": number,
                    "title": f"PR {number}",
                    "state": "MERGED",
                    "is_draft": False,
                    "created_at": utc(2026, 10, 1),
                    "updated_at": utc(2026, 10, 20),
                    "merged_at": utc(2026, 10, 19),
                    "closed_at": utc(2026, 10, 19),
                    "additions": 1,
                    "deletions": 1,
                    "author": "someone",
                    "author_type": "User",
                    "base_ref_name": "develop",
                    "merge_commit_sha": f"merge-{number}",  # cherry-picked under other SHAs
                    "first_commit_at": utc(2026, 10, 1),
                    "first_review_at": None,
                    "first_reviewer": None,
                    "first_approval_at": None,
                    "review_count": 0,
                    "closing_issues": [],
                    "labels": [],
                    "url": f"https://github.com/streamlit/streamlit/pull/{number}",
                    "raw": {},
                    "ingested_at": None,
                }
                for number in (17589, 17567)
            ],
        )
        counts = build_model(store, Config(), "streamlit/streamlit")
        changes = store.rows("deployment_changes")

    assert counts.changes == 2
    assert {(c["pr_number"], c["deploy_id"], c["matched_by"]) for c in changes} == {
        (17589, "R1", "pr_reference"),
        (17567, "R1", "pr_reference"),
    }
