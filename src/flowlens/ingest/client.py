"""GitHub API access: auth from the gh CLI and a client that retries and paces itself.

One client serves both APIs FlowLens uses: GraphQL for issues, PRs, releases, and
deployments, and REST for Actions workflow runs, which GraphQL doesn't expose.
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

API_URL = "https://api.github.com"
GRAPHQL_URL = f"{API_URL}/graphql"

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


class GitHubClient:
    """Sends GraphQL and REST requests, retries transient failures, and paces itself.

    ``calls`` counts HTTP requests (retries included) and ``cost`` sums the GraphQL
    rate-limit points GitHub charged, so callers can report what a run used. GraphQL and
    REST have separate hourly budgets, tracked in ``rate_limit`` and ``rest_rate_limit``.
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
            base_url=API_URL,
            headers={
                "Authorization": f"bearer {token}",
                "User-Agent": f"flowlens/{__version__}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
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
        self.rest_rate_limit: RateLimit | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def now(self) -> datetime:
        return self._now()

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a GraphQL query and return its ``data``."""
        response = self._send(
            "POST", GRAPHQL_URL, graphql=True, json={"query": query, "variables": variables}
        )
        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the token (HTTP 401). Run `gh auth login`.")
        if response.status_code != 200:
            raise GitHubError(f"HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if errors := payload.get("errors"):
            raise GitHubError("; ".join(e.get("message", str(e)) for e in errors))
        data = payload["data"]
        if rate_limit := data.get("rateLimit"):
            self.rate_limit = RateLimit(
                limit=rate_limit["limit"],
                remaining=rate_limit["remaining"],
                cost=rate_limit["cost"],
                reset_at=_parse_iso(rate_limit["resetAt"]),
            )
            self.cost += rate_limit["cost"]
            self._pace(self.rate_limit)
        return data

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET a REST path (or an absolute next-page URL) and return the response."""
        response = self._send("GET", url, graphql=False, params=params)
        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the token (HTTP 401). Run `gh auth login`.")
        if response.status_code == 404:
            raise GitHubError(f"Not found: {response.request.url.path}")
        if not response.is_success:
            raise GitHubError(f"HTTP {response.status_code}: {response.text[:300]}")
        headers = response.headers
        if headers.get("x-ratelimit-remaining", "").isdigit():
            self.rest_rate_limit = RateLimit(
                limit=int(headers.get("x-ratelimit-limit", "0")),
                remaining=int(headers["x-ratelimit-remaining"]),
                cost=1,
                reset_at=datetime.fromtimestamp(int(headers.get("x-ratelimit-reset", "0")), UTC),
            )
            self._pace(self.rest_rate_limit)
        return response

    def get_pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Yield each page of a paginated REST endpoint, following ``Link: rel="next"``."""
        url: str | None = path
        while url:
            response = self.get(url, params)
            yield response.json()
            next_link = response.links.get("next")
            url, params = (next_link["url"], None) if next_link else (None, None)

    def _send(self, method: str, url: str, *, graphql: bool, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying with backoff until it succeeds or can't be retried."""
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                wait, reason = self._backoff(attempt), f"network error ({type(exc).__name__})"
            else:
                wait, reason = self._retry_wait(response, attempt, graphql=graphql)
                if wait is None:
                    return response
            if attempt == self.max_retries:
                raise GitHubError(f"Giving up after {attempt + 1} attempts: {reason}.")
            self._log(f"{reason}; retrying in {wait:.0f}s")
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _retry_wait(
        self, response: httpx.Response, attempt: int, *, graphql: bool
    ) -> tuple[float | None, str]:
        """How long to wait before retrying this response, or None if it shouldn't be."""
        status = response.status_code
        if status == 200:
            if graphql and _graphql_rate_limited(response):
                return self._seconds_until_reset(response), "GraphQL rate limit exhausted"
            return None, ""
        if status == 429 or (status == 403 and _looks_rate_limited(response)):
            return self._rate_limit_wait(response, attempt), f"HTTP {status} rate limited"
        if status in RETRYABLE_STATUSES:
            return self._backoff(attempt), f"HTTP {status}"
        return None, ""

    def _pace(self, rate_limit: RateLimit) -> None:
        """Sleep until the budget resets when it's nearly spent, instead of hitting 403s."""
        if rate_limit.remaining >= self.min_remaining:
            return
        wait = max(0.0, (rate_limit.reset_at - self._now()).total_seconds()) + 1
        self._log(f"{rate_limit.remaining} rate-limit points left; pausing {wait:.0f}s until reset")
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


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
