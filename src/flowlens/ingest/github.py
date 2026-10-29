"""GitHub GraphQL access: auth, a rate-limit-aware client, pagination, and row mapping.

Issues and pull requests are both read newest-updated first. For issues GitHub filters by
``since`` server-side; pull requests have no such filter, so we stop paging at the first PR
older than ``since``. Updating an item during a crawl only moves it to the front of the
ordering, so a crawl can repeat items but never skips one that hasn't changed.
"""

import random
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from flowlens import __version__

GRAPHQL_URL = "https://api.github.com/graphql"

ISSUE_PAGE_SIZE = 100
# PR nodes carry commits, reviews, and diff stats; GitHub times out (502) more often on
# big PR pages, so they are fetched in smaller pages.
PULL_REQUEST_PAGE_SIZE = 50

ISSUES_QUERY = """
query Issues($owner: String!, $name: String!, $first: Int!, $cursor: String, $since: DateTime) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issues(first: $first, after: $cursor, filterBy: {since: $since},
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        url
        state
        stateReason
        createdAt
        updatedAt
        closedAt
        author { login __typename }
        labels(first: 50) { nodes { name } }
        assignees(first: 20) { nodes { login } }
      }
    }
  }
}
"""

PULL_REQUESTS_QUERY = """
query PullRequests($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        url
        state
        isDraft
        createdAt
        updatedAt
        mergedAt
        closedAt
        additions
        deletions
        baseRefName
        author { login __typename }
        labels(first: 50) { nodes { name } }
        commits(first: 1) { nodes { commit { authoredDate committedDate } } }
        reviews(first: 20) { nodes { submittedAt author { login __typename } } }
      }
    }
  }
}
"""

RETRYABLE_STATUSES = {500, 502, 503, 504}
# GitHub asks clients to wait at least a minute on a secondary rate limit with no headers.
SECONDARY_LIMIT_WAIT = 60.0


class GitHubError(RuntimeError):
    """GitHub returned something FlowLens can't recover from."""


class GitHubAuthError(GitHubError):
    """No usable token, or GitHub rejected it."""


def token_from_gh() -> str:
    """Return the token the GitHub CLI is logged in with. It is kept in memory only."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True, timeout=15
        )
    except FileNotFoundError as exc:
        raise GitHubAuthError(
            "GitHub CLI `gh` not found. Install it and run `gh auth login`."
        ) from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GitHubAuthError("`gh auth token` failed. Run `gh auth login` first.") from exc
    token = result.stdout.strip()
    if not token:
        raise GitHubAuthError("`gh auth token` printed nothing. Run `gh auth login` first.")
    return token


@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    cost: int
    reset_at: datetime


class GraphQLClient:
    """POSTs GraphQL queries, retries transient failures, and paces itself on rate limits.

    ``calls`` counts HTTP requests (retries included) and ``cost`` sums the rate-limit points
    GitHub charged, so callers can report what a run used.
    """

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        log: Callable[[str], None] | None = None,
        max_retries: int = 6,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        min_remaining: int = 50,
        timeout: float = 60.0,
    ) -> None:
        self._http = httpx.Client(
            headers={
                "Authorization": f"bearer {token}",
                "User-Agent": f"flowlens/{__version__}",
            },
            transport=transport,
            timeout=timeout,
        )
        self._sleep = sleep
        self._now = now
        self._log = log or (lambda _message: None)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.min_remaining = min_remaining
        self.calls = 0
        self.cost = 0
        self.rate_limit: RateLimit | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GraphQLClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a query and return its ``data``, retrying with exponential backoff."""
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            try:
                response = self._http.post(
                    GRAPHQL_URL, json={"query": query, "variables": variables}
                )
            except httpx.TransportError as exc:
                wait, reason = self._backoff(attempt), f"network error ({type(exc).__name__})"
            else:
                wait, reason = self._retry_wait(response, attempt)
                if wait is None:
                    return self._parse(response)
            if attempt == self.max_retries:
                raise GitHubError(f"Giving up after {attempt + 1} attempts: {reason}.")
            self._log(f"{reason}; retrying in {wait:.0f}s")
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _retry_wait(self, response: httpx.Response, attempt: int) -> tuple[float | None, str]:
        """How long to wait before retrying this response, or None if it shouldn't be."""
        status = response.status_code
        if status == 200:
            if _graphql_rate_limited(response):
                return self._seconds_until_reset(response), "GraphQL rate limit exhausted"
            return None, ""
        if status == 429 or (status == 403 and _looks_rate_limited(response)):
            return self._rate_limit_wait(response, attempt), f"HTTP {status} rate limited"
        if status in RETRYABLE_STATUSES:
            return self._backoff(attempt), f"HTTP {status}"
        return None, ""

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the token (HTTP 401). Run `gh auth login`.")
        if response.status_code != 200:
            raise GitHubError(f"HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if errors := payload.get("errors"):
            raise GitHubError("; ".join(e.get("message", str(e)) for e in errors))
        data = payload["data"]
        self._track(data.get("rateLimit"))
        return data

    def _track(self, rate_limit: dict[str, Any] | None) -> None:
        if not rate_limit:
            return
        self.rate_limit = RateLimit(
            limit=rate_limit["limit"],
            remaining=rate_limit["remaining"],
            cost=rate_limit["cost"],
            reset_at=parse_ts(rate_limit["resetAt"]),
        )
        self.cost += rate_limit["cost"]
        if self.rate_limit.remaining < self.min_remaining:
            wait = max(0.0, (self.rate_limit.reset_at - self._now()).total_seconds()) + 1
            self._log(
                f"{self.rate_limit.remaining} rate-limit points left; "
                f"pausing {wait:.0f}s until reset"
            )
            self._sleep(wait)

    def _backoff(self, attempt: int) -> float:
        delay = self.backoff_base * 2**attempt
        return min(self.backoff_cap, delay * (1 + 0.25 * random.random()))

    def _rate_limit_wait(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            return float(retry_after)
        reset = response.headers.get("x-ratelimit-reset", "")
        if response.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
            return max(0.0, int(reset) - self._now().timestamp()) + 1
        return max(SECONDARY_LIMIT_WAIT, self._backoff(attempt))

    def _seconds_until_reset(self, response: httpx.Response) -> float:
        reset = response.headers.get("x-ratelimit-reset", "")
        if reset.isdigit():
            return max(0.0, int(reset) - self._now().timestamp()) + 1
        if self.rate_limit is not None:
            return max(0.0, (self.rate_limit.reset_at - self._now()).total_seconds()) + 1
        return SECONDARY_LIMIT_WAIT


def _looks_rate_limited(response: httpx.Response) -> bool:
    """403 means "rate limited" only with these signals; otherwise it's a permissions error."""
    headers = response.headers
    return (
        "retry-after" in headers
        or headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in response.text.lower()
    )


def _graphql_rate_limited(response: httpx.Response) -> bool:
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        return False
    return any(error.get("type") == "RATE_LIMITED" for error in errors)


def paginate(
    client: GraphQLClient, query: str, variables: dict[str, Any], connection: str
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of nodes from ``repository.<connection>``, following ``endCursor``."""
    cursor: str | None = None
    while True:
        data = client.query(query, {**variables, "cursor": cursor})
        repository = data.get("repository")
        if repository is None:
            raise GitHubError(
                f"Repository {variables['owner']}/{variables['name']} not found or not visible."
            )
        page = repository[connection]
        yield [node for node in page["nodes"] if node]
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


def fetch_issues(
    client: GraphQLClient,
    owner: str,
    name: str,
    since: datetime | None = None,
    page_size: int = ISSUE_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of issues updated at or after ``since`` (all issues when None)."""
    variables = {"owner": owner, "name": name, "first": page_size, "since": format_ts(since)}
    yield from paginate(client, ISSUES_QUERY, variables, "issues")


def fetch_pull_requests(
    client: GraphQLClient,
    owner: str,
    name: str,
    since: datetime | None = None,
    page_size: int = PULL_REQUEST_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of PRs updated at or after ``since``, stopping once older ones appear."""
    variables = {"owner": owner, "name": name, "first": page_size}
    for nodes in paginate(client, PULL_REQUESTS_QUERY, variables, "pullRequests"):
        if since is None:
            yield nodes
            continue
        fresh = [node for node in nodes if parse_ts(node["updatedAt"]) >= since]
        if fresh:
            yield fresh
        if len(fresh) < len(nodes):
            return


def issue_row(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten an issue node into an ``issues`` row. ``raw`` keeps the node as fetched."""
    return {
        "number": node["number"],
        "title": node["title"],
        "state": node["state"],
        "state_reason": node.get("stateReason"),
        "created_at": parse_ts(node["createdAt"]),
        "updated_at": parse_ts(node["updatedAt"]),
        "closed_at": parse_ts(node.get("closedAt")),
        "author": _login(node.get("author")),
        "author_type": _actor_type(node.get("author")),
        "labels": [label["name"] for label in _nodes(node.get("labels"))],
        "assignees": [user["login"] for user in _nodes(node.get("assignees"))],
        "url": node["url"],
        "raw": node,
    }


def pull_request_row(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten a PR node into a ``pull_requests`` row. ``raw`` keeps the node as fetched."""
    author = _login(node.get("author"))
    commits = _nodes(node.get("commits"))
    first_review_at, first_reviewer = first_review(_nodes(node.get("reviews")), author)
    return {
        "number": node["number"],
        "title": node["title"],
        "state": node["state"],
        "is_draft": node.get("isDraft"),
        "created_at": parse_ts(node["createdAt"]),
        "updated_at": parse_ts(node["updatedAt"]),
        "merged_at": parse_ts(node.get("mergedAt")),
        "closed_at": parse_ts(node.get("closedAt")),
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "author": author,
        "author_type": _actor_type(node.get("author")),
        "base_ref_name": node.get("baseRefName"),
        "first_commit_at": parse_ts(commits[0]["commit"]["authoredDate"]) if commits else None,
        "first_review_at": first_review_at,
        "first_reviewer": first_reviewer,
        "labels": [label["name"] for label in _nodes(node.get("labels"))],
        "url": node["url"],
        "raw": node,
    }


def first_review(
    reviews: list[dict[str, Any]], pr_author: str | None
) -> tuple[datetime | None, str | None]:
    """Earliest submitted review by someone other than the PR author.

    Replying to a review thread on your own PR creates a review authored by you, so the
    author's own reviews are skipped. Unsubmitted (pending) reviews have no timestamp.
    """
    earliest: tuple[datetime, str | None] | None = None
    for review in reviews:
        submitted = parse_ts(review.get("submittedAt"))
        if submitted is None:
            continue
        reviewer = _login(review.get("author"))
        if pr_author is not None and reviewer == pr_author:
            continue
        if earliest is None or submitted < earliest[0]:
            earliest = (submitted, reviewer)
    return earliest if earliest else (None, None)


def parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime."""
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def format_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not connection:
        return []
    return [node for node in connection.get("nodes") or [] if node]


def _login(actor: dict[str, Any] | None) -> str | None:
    # A deleted account comes back as a null actor (GitHub shows it as "ghost").
    return actor["login"] if actor else None


def _actor_type(actor: dict[str, Any] | None) -> str | None:
    return actor.get("__typename") if actor else None
