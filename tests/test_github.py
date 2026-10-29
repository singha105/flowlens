import subprocess
from datetime import UTC, datetime, timedelta
from itertools import islice

import httpx
import pytest

from fakes import NOW, FakeGitHub, in_sequence, load_fixture, recorded_pages
from flowlens.ingest import github
from flowlens.ingest.github import (
    GitHubAuthError,
    GitHubError,
    fetch_issues,
    fetch_pull_requests,
    first_review,
    issue_row,
    pull_request_row,
)

OK = load_fixture("issues_page3.json")


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


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
    assert client.cost == 3
    assert client.rate_limit.remaining == 4989


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


# --- row mapping ------------------------------------------------------------------------


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
    assert row["raw"] is node


def test_pull_request_row_uses_the_first_commits_author_date():
    nodes = load_fixture("pull_requests_page2.json")["data"]["repository"]["pullRequests"]["nodes"]
    node = next(n for n in nodes if n["number"] == 514)

    row = pull_request_row(node)

    assert row["state"] == "MERGED"
    assert row["merged_at"] is not None
    assert row["base_ref_name"] == "stable"
    # Authored in February, rebased (committed) in October: the work started in February.
    assert row["first_commit_at"] == utc(2026, 2, 9, 10, 41, 20)
    assert row["first_review_at"] is None
    assert row["first_reviewer"] is None
    assert isinstance(row["additions"], int)


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


# --- retries and rate limits ------------------------------------------------------------


def test_502_is_retried_with_exponential_backoff():
    fake = FakeGitHub(in_sequence(httpx.Response(502), httpx.Response(502), OK))
    client = fake.client()

    client.query("query", {})

    assert client.calls == 3
    assert len(fake.sleeps) == 2
    assert 1.0 <= fake.sleeps[0] <= 1.25
    assert 2.0 <= fake.sleeps[1] <= 2.5


def test_secondary_rate_limit_honours_retry_after():
    limited = httpx.Response(
        403,
        headers={"retry-after": "7"},
        json={"message": "You have exceeded a secondary rate limit."},
    )
    fake = FakeGitHub(in_sequence(limited, OK))

    fake.client().query("query", {})

    assert fake.sleeps == [7.0]


def test_secondary_rate_limit_without_headers_waits_at_least_a_minute():
    limited = httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."})
    fake = FakeGitHub(in_sequence(limited, OK))

    fake.client().query("query", {})

    assert fake.sleeps == [60.0]


def test_429_waits_until_the_rate_limit_resets():
    reset = int((NOW + timedelta(seconds=30)).timestamp())
    limited = httpx.Response(
        429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
    )
    fake = FakeGitHub(in_sequence(limited, OK))

    fake.client().query("query", {})

    assert fake.sleeps == [31.0]


def test_graphql_rate_limited_error_waits_and_retries():
    reset = int((NOW + timedelta(seconds=120)).timestamp())
    limited = httpx.Response(
        200,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        json={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
    )
    fake = FakeGitHub(in_sequence(limited, OK))

    data = fake.client().query("query", {})

    assert fake.sleeps == [121.0]
    assert data["repository"]["issues"]["nodes"]


def test_403_without_rate_limit_signals_is_not_retried():
    forbidden = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    fake = FakeGitHub(in_sequence(forbidden))
    client = fake.client()

    with pytest.raises(GitHubError, match="HTTP 403"):
        client.query("query", {})
    assert fake.sleeps == []
    assert client.calls == 1


def test_401_raises_an_auth_error():
    fake = FakeGitHub(in_sequence(httpx.Response(401, json={"message": "Bad credentials"})))

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        fake.client().query("query", {})


def test_network_errors_are_retried():
    fake = FakeGitHub(in_sequence(httpx.ConnectError("connection reset"), OK))
    client = fake.client()

    client.query("query", {})

    assert client.calls == 2
    assert len(fake.sleeps) == 1


def test_gives_up_after_max_retries():
    fake = FakeGitHub(lambda _body: httpx.Response(502))
    client = fake.client(max_retries=2)

    with pytest.raises(GitHubError, match="Giving up after 3 attempts: HTTP 502"):
        client.query("query", {})
    assert client.calls == 3
    assert len(fake.sleeps) == 2


def test_pauses_until_reset_when_few_points_remain():
    payload = load_fixture("issues_page3.json")
    payload["data"]["rateLimit"]["remaining"] = 10
    payload["data"]["rateLimit"]["resetAt"] = "2026-10-29T05:01:00Z"  # NOW + 60s
    fake = FakeGitHub(in_sequence(payload))

    fake.client(min_remaining=50).query("query", {})

    assert fake.sleeps == [61.0]


# --- auth -------------------------------------------------------------------------------


def test_token_comes_from_gh(monkeypatch: pytest.MonkeyPatch):
    def fake_run(args, **_kwargs):
        assert args == ["gh", "auth", "token"]
        return subprocess.CompletedProcess(args, 0, stdout="gho_example\n", stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.token_from_gh() == "gho_example"


def test_missing_gh_is_an_auth_error(monkeypatch: pytest.MonkeyPatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    with pytest.raises(GitHubAuthError, match="not found"):
        github.token_from_gh()


def test_logged_out_gh_is_an_auth_error(monkeypatch: pytest.MonkeyPatch):
    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        github.token_from_gh()
