import copy
from datetime import UTC, datetime
from itertools import islice

import pytest

from fakes import FakeGitHub, FakeRepo, fixture_items, load_fixture, recorded_pages
from flowlens.ingest.client import GitHubError
from flowlens.ingest.github import (
    ISSUES_QUERY,
    PULL_REQUESTS_QUERY,
    fetch_issues,
    fetch_pull_requests,
    first_review,
    issue_event_rows,
    issue_row,
    pull_request_row,
    review_rows,
)

STREAMLIT = "streamlit/streamlit"


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


@pytest.fixture(scope="module")
def streamlit_issues():
    return fixture_items("streamlit_issues.json")


@pytest.fixture(scope="module")
def streamlit_prs():
    return fixture_items("streamlit_prs.json")


def events(node, event_type=None):
    rows = issue_event_rows(node)
    return [row for row in rows if event_type is None or row["event_type"] == event_type]


# --- pagination -------------------------------------------------------------------------


def test_issues_follow_cursors_until_the_last_page():
    fake = FakeGitHub(recorded_pages("issues", 3))
    client = fake.client()

    pages = list(fetch_issues(client, "pallets", "markupsafe", utc(2026, 7, 1), page_size=3))

    assert [len(page) for page in pages] == [3, 3, 2]
    assert [n["number"] for page in pages for n in page] == [533, 517, 515, 530, 531, 521, 540, 509]
    page1 = load_fixture("issues_page1.json")["data"]["repository"]["issues"]
    page2 = load_fixture("issues_page2.json")["data"]["repository"]["issues"]
    assert fake.cursors == [None, page1["pageInfo"]["endCursor"], page2["pageInfo"]["endCursor"]]
    assert {body["variables"]["since"] for body in fake.requests} == {"2026-07-01T00:00:00Z"}
    assert {body["variables"]["first"] for body in fake.requests} == {3}
    assert client.calls == 3


def test_issues_without_since_send_a_null_filter():
    fake = FakeGitHub(recorded_pages("issues", 3))

    list(fetch_issues(fake.client(), "pallets", "markupsafe"))

    assert fake.requests[0]["variables"]["since"] is None


def test_pull_requests_stop_at_the_first_item_older_than_since():
    fake = FakeGitHub(recorded_pages("pull_requests", 2))
    since = utc(2026, 10, 17, 0, 9, 18)  # PR 514's updatedAt, on page 2

    pages = list(fetch_pull_requests(fake.client(), "pallets", "markupsafe", since, page_size=4))

    numbers = [n["number"] for page in pages for n in page]
    assert numbers == [519, 539, 534, 535, 510, 514]  # 514 is equal to since, so it's kept
    assert len(fake.requests) == 2  # page 2 says hasNextPage, but everything after is older


def test_pagination_is_lazy():
    fake = FakeGitHub(recorded_pages("pull_requests", 2))

    pages = list(islice(fetch_pull_requests(fake.client(), "pallets", "markupsafe"), 2))

    assert [len(page) for page in pages] == [4, 4]
    assert len(fake.requests) == 2


def test_missing_repository_raises_a_clear_error():
    fake = FakeGitHub(lambda _body: load_fixture("repo_not_found.json"))

    with pytest.raises(GitHubError, match="Could not resolve to a Repository"):
        list(fetch_issues(fake.client(), "singha105", "no-such-repo-flowlens"))


def test_queries_only_declare_fragments_they_use():
    # GraphQL rejects a document with an unused fragment, so each query carries exactly
    # the fragments it spreads.
    for query in (ISSUES_QUERY, PULL_REQUESTS_QUERY):
        declared = {line.split()[1] for line in query.splitlines() if line.startswith("fragment")}
        for fragment in declared:
            assert f"...{fragment}" in query


def test_a_capped_timeline_is_completed_with_follow_up_queries(streamlit_issues):
    node = streamlit_issues[8793]  # 14 timeline items
    repo = FakeRepo()
    repo.issues = [repo.cap(node, "timelineItems", keep=5)]
    fake = FakeGitHub(repo)

    (page,) = fetch_issues(fake.client(), "streamlit", "streamlit")

    assert len(page[0]["timelineItems"]["nodes"]) == len(node["timelineItems"]["nodes"])
    assert page[0]["timelineItems"]["pageInfo"]["hasNextPage"] is False
    follow_up = fake.requests[1]
    assert "node(id:" in follow_up["query"]
    assert follow_up["variables"] == {"id": node["id"], "cursor": "5"}
    assert events(page[0]) == events(node)


def test_capped_reviews_are_completed_with_follow_up_queries(streamlit_prs):
    node = streamlit_prs[17730]  # six reviews
    repo = FakeRepo()
    repo.pull_requests = [repo.cap(node, "reviews", keep=1)]
    fake = FakeGitHub(repo)

    (page,) = fetch_pull_requests(fake.client(), "streamlit", "streamlit")

    assert len(page[0]["reviews"]["nodes"]) == 6
    assert len(fake.requests) == 2
    # The first review (a bot's) was the only one fetched up front; the human approval
    # that counts came in the follow-up.
    assert pull_request_row(page[0], STREAMLIT)["first_review_at"] == utc(2026, 10, 28, 20, 41, 12)


def test_a_closed_issue_missing_its_close_event_is_read_again(streamlit_issues):
    # GitHub drops events from big issue pages without saying so; a closed issue with no
    # close event is the tell.
    node = streamlit_issues[11752]  # five events, the close last
    repo = FakeRepo()
    repo.issues = [repo.cap(node, "timelineItems", keep=2, silently=True)]
    fake = FakeGitHub(repo)

    (page,) = fetch_issues(fake.client(), "streamlit", "streamlit")

    assert fake.requests[1]["variables"] == {"id": node["id"], "cursor": None}
    assert events(page[0]) == events(node)


def test_open_issues_and_complete_timelines_are_not_read_again(streamlit_issues):
    repo = FakeRepo()
    repo.issues = [
        repo.cap(streamlit_issues[16726], "timelineItems", keep=3, silently=True),  # open
        streamlit_issues[17733],  # closed, with its close event
    ]
    fake = FakeGitHub(repo)

    list(fetch_issues(fake.client(), "streamlit", "streamlit"))

    assert len(fake.requests) == 1


def test_a_closed_issue_with_no_close_event_at_all_is_read_once_more_only(streamlit_issues):
    node = copy.deepcopy(streamlit_issues[11752])
    node["timelineItems"]["nodes"] = [
        e for e in node["timelineItems"]["nodes"] if e["__typename"] != "ClosedEvent"
    ]
    kept = len(node["timelineItems"]["nodes"])
    repo = FakeRepo()
    repo.issues = [repo.cap(node, "timelineItems", keep=kept)]
    fake = FakeGitHub(repo)

    (page,) = fetch_issues(fake.client(), "streamlit", "streamlit")

    assert len(fake.requests) == 2  # the page, then one re-read that changes nothing
    assert len(page[0]["timelineItems"]["nodes"]) == kept


# --- issues and their timelines ---------------------------------------------------------


def test_issue_row_flattens_a_recorded_node():
    node = load_fixture("issues_page1.json")["data"]["repository"]["issues"]["nodes"][0]

    row = issue_row(node)

    assert row["number"] == 533
    assert row["state"] == "CLOSED"
    assert row["state_reason"] == "NOT_PLANNED"
    assert row["created_at"] == utc(2026, 10, 1, 14, 51, 43)
    assert row["closed_at"] == utc(2026, 10, 3, 13, 45, 10)
    assert row["author"] == "davidism"
    assert row["author_type"] == "User"
    assert row["assignees"] == ["davidism"]
    assert row["labels"] == []
    assert "timelineItems" not in row["raw"]  # events are stored as their own rows


def test_assigned_and_unassigned_events(streamlit_issues):
    node = streamlit_issues[16726]

    (assigned,) = events(node, "assigned")
    (unassigned,) = events(node, "unassigned")

    assert (assigned["created_at"], assigned["assignee"], assigned["actor"]) == (
        utc(2026, 8, 30, 15, 11, 5),
        "sfc-gh-lmasuch",
        "sfc-gh-lmasuch",
    )
    assert (unassigned["assignee"], unassigned["actor"]) == ("sfc-gh-lmasuch", "lukasmasuch")
    assert issue_row(node)["assignees"] == []  # current state: nobody


def test_labeled_and_unlabeled_events(streamlit_issues):
    node = streamlit_issues[14555]

    labeled = [(e["label"], e["actor"]) for e in events(node, "labeled")]
    unlabeled = [e["label"] for e in events(node, "unlabeled")]

    assert labeled[0] == ("type:bug", "ghilesmeddour")
    assert ("type:enhancement", "lukasmasuch") in labeled
    assert "type:bug" in unlabeled  # relabelled from bug to enhancement
    assert "type:bug" not in issue_row(node)["labels"]


def test_closed_event_keeps_its_state_reason_and_closing_pr(streamlit_issues):
    (closed,) = events(streamlit_issues[11752], "closed")

    assert closed["state_reason"] == "COMPLETED"
    assert (closed["pr_number"], closed["pr_repo"]) == (16418, STREAMLIT)
    assert closed["pr_created_at"] == utc(2026, 8, 8, 12, 20, 26)


def test_closed_not_planned(streamlit_issues):
    node = streamlit_issues[14209]

    (closed,) = events(node, "closed")

    assert closed["state_reason"] == "NOT_PLANNED"
    assert closed["pr_number"] is None
    assert issue_row(node)["state_reason"] == "NOT_PLANNED"


def test_reopened_events(streamlit_issues):
    node = streamlit_issues[8793]

    kinds = [e["event_type"] for e in events(node) if e["event_type"] in {"closed", "reopened"}]

    assert kinds == ["closed", "reopened", "closed"]
    still_open = streamlit_issues[8271]
    assert [e["event_type"] for e in events(still_open, "reopened")] == ["reopened"]
    assert (issue_row(still_open)["state"], issue_row(still_open)["state_reason"]) == (
        "OPEN",
        "REOPENED",
    )


def test_cross_references_keep_the_pr_and_its_repo(streamlit_issues):
    refs = events(streamlit_issues[12801], "cross_referenced")

    assert [(e["pr_number"], e["pr_repo"]) for e in refs] == [
        (1382, "streamlit/docs"),
        (9, "mrdsantos/insightflow"),
    ]
    assert {e["will_close"] for e in refs} == {False}


def test_cross_references_from_issues_are_dropped():
    node = copy.deepcopy(fixture_items("streamlit_issues.json")[12801])
    mention = copy.deepcopy(node["timelineItems"]["nodes"][-1])
    mention["id"] = "CRE_from_an_issue"
    mention["source"] = {"__typename": "Issue", "number": 1}
    node["timelineItems"]["nodes"].append(mention)

    assert "CRE_from_an_issue" not in {e["id"] for e in events(node)}


def test_connected_event():
    node = fixture_items("cli_issues.json")[14614]

    (connected,) = events(node, "connected")

    assert (connected["pr_number"], connected["pr_repo"]) == (14616, "cli/cli")
    assert connected["pr_created_at"] == utc(2026, 10, 7, 8, 24, 38)


def test_every_tracked_event_type_is_parsed(streamlit_issues):
    seen = {e["event_type"] for node in streamlit_issues.values() for e in events(node)}
    seen |= {e["event_type"] for e in events(fixture_items("cli_issues.json")[14614])}

    assert seen == {
        "assigned",
        "unassigned",
        "labeled",
        "unlabeled",
        "cross_referenced",
        "connected",
        "closed",
        "reopened",
    }


# --- pull requests and reviews ----------------------------------------------------------


def test_pull_request_row_uses_the_first_commits_author_date():
    nodes = load_fixture("pull_requests_page2.json")["data"]["repository"]["pullRequests"]["nodes"]
    node = next(n for n in nodes if n["number"] == 514)

    row = pull_request_row(node, "pallets/markupsafe")

    assert row["state"] == "MERGED"
    assert row["base_ref_name"] == "stable"
    # Authored in February, rebased (committed) in October: the work started in February.
    assert row["first_commit_at"] == utc(2026, 2, 9, 10, 41, 20)
    assert row["first_review_at"] is None
    assert isinstance(row["additions"], int)


def test_first_review_and_approval_skip_bots_and_the_author(streamlit_prs):
    row = pull_request_row(streamlit_prs[17730], STREAMLIT)

    # Copilot commented 11s after the PR opened and github-actions approved later; neither
    # counts. The author's own reply is a review too, and doesn't count either.
    assert row["first_review_at"] == utc(2026, 10, 28, 20, 41, 12)
    assert row["first_reviewer"] == "sfc-gh-lmasuch"
    assert row["first_approval_at"] == utc(2026, 10, 28, 20, 41, 12)
    assert row["review_count"] == 1
    assert row["merge_commit_sha"] == "95e2aa71230cd09c7736a98f4e258933e8fd54b0"
    assert "reviews" not in row["raw"]


def test_a_bot_approval_is_not_an_approval(streamlit_prs):
    row = pull_request_row(streamlit_prs[17704], STREAMLIT)

    assert row["first_reviewer"] == "sfc-gh-nbellante"
    assert row["first_approval_at"] is None
    assert row["merge_commit_sha"] is None  # still open


def test_a_pr_reviewed_only_by_bots_and_its_author_has_no_review(streamlit_prs):
    row = pull_request_row(streamlit_prs[17688], STREAMLIT)

    assert (row["first_review_at"], row["first_approval_at"], row["review_count"]) == (
        None,
        None,
        0,
    )
    assert row["closing_issues"] == [17612]


def test_review_rows_keep_every_review_with_its_author_type(streamlit_prs):
    rows = review_rows(streamlit_prs[17730])

    assert len(rows) == 6
    assert [(r["author"], r["author_type"], r["state"]) for r in rows[:2]] == [
        ("copilot-pull-request-reviewer", "Bot", "COMMENTED"),
        ("sfc-gh-lmasuch", "User", "APPROVED"),
    ]
    assert {r["pr_number"] for r in rows} == {17730}


def test_changes_requested_then_approved():
    node = fixture_items("cli_prs.json")[11926]

    row = pull_request_row(node, "cli/cli")
    states = [r["state"] for r in review_rows(node) if r["author"] == "BagToad"]

    assert states == ["APPROVED", "CHANGES_REQUESTED", "APPROVED"]
    assert row["first_reviewer"] == "babakks"
    assert row["first_approval_at"] == utc(2025, 10, 31, 18, 18, 48)


def test_bot_authored_pr(streamlit_prs):
    row = pull_request_row(streamlit_prs[17735], STREAMLIT)

    assert (row["author"], row["author_type"]) == ("github-actions", "Bot")


def test_closing_issues_in_other_repos_are_dropped(streamlit_prs):
    node = copy.deepcopy(streamlit_prs[17688])
    node["closingIssuesReferences"]["nodes"].append(
        {"number": 99, "repository": {"nameWithOwner": "streamlit/docs"}}
    )

    assert pull_request_row(node, STREAMLIT)["closing_issues"] == [17612]


def test_first_review_skips_the_authors_own_and_pending_reviews():
    reviews = [
        {"submittedAt": "2026-01-01T09:00:00Z", "author": {"login": "alice"}},  # self
        {"submittedAt": None, "author": {"login": "carol"}},  # pending
        {"submittedAt": "2026-01-03T09:00:00Z", "author": {"login": "bob"}},
        {"submittedAt": "2026-01-02T09:00:00Z", "author": {"login": "dave"}},
    ]

    assert first_review(reviews, "alice") == (utc(2026, 1, 2, 9), "dave")


def test_first_review_counts_reviews_from_deleted_accounts():
    reviews = [{"submittedAt": "2026-01-02T09:00:00Z", "author": None}]

    assert first_review(reviews, "alice") == (utc(2026, 1, 2, 9), None)
    assert first_review([], "alice") == (None, None)
