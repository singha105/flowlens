import subprocess
from datetime import timedelta

import httpx
import pytest

from fakes import NOW, FakeGitHub, in_sequence, load_fixture
from flowlens.ingest import client as client_module
from flowlens.ingest.client import API_URL, GitHubAuthError, GitHubError, token_from_gh

OK = load_fixture("issues_page3.json")


# --- GraphQL retries and rate limits ----------------------------------------------------


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


def test_graphql_cost_and_budget_are_tracked():
    fake = FakeGitHub(in_sequence(OK, OK))
    client = fake.client()

    client.query("query", {})
    client.query("query", {})

    assert (client.calls, client.cost) == (2, 2)
    assert client.rate_limit.remaining == OK["data"]["rateLimit"]["remaining"]


# --- REST -------------------------------------------------------------------------------


def rest_page(items: list[int], next_page: int | None = None, remaining: int = 4999):
    headers = {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int((NOW + timedelta(seconds=90)).timestamp())),
    }
    if next_page:
        headers["Link"] = (
            f'<{API_URL}/repos/o/r/things?per_page=2&page={next_page}>; rel="next", '
            f'<{API_URL}/repos/o/r/things?per_page=2&page=9>; rel="last"'
        )
    return httpx.Response(200, json={"items": items}, headers=headers)


def test_rest_pages_follow_the_link_header():
    fake = FakeGitHub(rest=in_sequence(rest_page([1, 2], 2), rest_page([3, 4], 3), rest_page([5])))
    client = fake.client()

    pages = list(client.get_pages("/repos/o/r/things", {"per_page": 2}))

    assert [page["items"] for page in pages] == [[1, 2], [3, 4], [5]]
    assert fake.rest_requests == [
        ("/repos/o/r/things", {"per_page": "2"}),
        ("/repos/o/r/things", {"per_page": "2", "page": "2"}),
        ("/repos/o/r/things", {"per_page": "2", "page": "3"}),
    ]
    assert client.calls == 3
    assert client.cost == 0  # REST doesn't spend GraphQL points
    assert client.rest_rate_limit.remaining == 4999


def test_rest_paces_on_its_own_budget():
    fake = FakeGitHub(rest=in_sequence(rest_page([1], remaining=3)))

    fake.client(min_remaining=50).get("/repos/o/r/things")

    assert fake.sleeps == [91.0]


def test_rest_retries_server_errors():
    fake = FakeGitHub(rest=in_sequence(httpx.Response(503), rest_page([1])))
    client = fake.client()

    assert client.get("/repos/o/r/things").json() == {"items": [1]}
    assert client.calls == 2


def test_rest_404_names_the_path():
    fake = FakeGitHub(rest=in_sequence(httpx.Response(404, json={"message": "Not Found"})))

    with pytest.raises(GitHubError, match="Not found: /repos/o/r/things"):
        fake.client().get("/repos/o/r/things")


# --- auth -------------------------------------------------------------------------------


def test_token_comes_from_gh(monkeypatch: pytest.MonkeyPatch):
    def fake_run(args, **_kwargs):
        assert args == ["gh", "auth", "token"]
        return subprocess.CompletedProcess(args, 0, stdout="gho_example\n", stderr="")

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    assert token_from_gh() == "gho_example"


def test_missing_gh_is_an_auth_error(monkeypatch: pytest.MonkeyPatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    with pytest.raises(GitHubAuthError, match="not found"):
        token_from_gh()


def test_logged_out_gh_is_an_auth_error(monkeypatch: pytest.MonkeyPatch):
    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        token_from_gh()
