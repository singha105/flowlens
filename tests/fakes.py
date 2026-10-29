"""Offline stand-ins for the GitHub GraphQL API, built on recorded fixtures."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from flowlens.ingest.github import GraphQLClient

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)

Responder = Callable[[dict[str, Any]], httpx.Response | dict[str, Any]]


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def fixture_nodes(prefix: str, pages: int) -> list[dict[str, Any]]:
    nodes = []
    for page in range(1, pages + 1):
        repository = load_fixture(f"{prefix}_page{page}.json")["data"]["repository"]
        nodes.extend(next(iter(repository.values()))["nodes"])
    return nodes


class FakeGitHub:
    """Stands in for api.github.com: records each GraphQL request and answers it."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self.sleeps: list[float] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        result = self.responder(body)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    def client(self, **options: Any) -> GraphQLClient:
        return GraphQLClient(
            "test-token",
            transport=httpx.MockTransport(self._handle),
            sleep=self.sleeps.append,
            now=lambda: NOW,
            **options,
        )

    @property
    def cursors(self) -> list[str | None]:
        return [body["variables"]["cursor"] for body in self.requests]


def recorded_pages(prefix: str, pages: int) -> Responder:
    """Serve recorded <prefix>_pageN.json files, chosen by the cursor in each request."""
    payloads = [load_fixture(f"{prefix}_page{page}.json") for page in range(1, pages + 1)]
    by_cursor: dict[str | None, dict[str, Any]] = {None: payloads[0]}
    for previous, following in pairwise(payloads):
        connection = next(iter(previous["data"]["repository"].values()))
        by_cursor[connection["pageInfo"]["endCursor"]] = following
    return lambda body: by_cursor[body["variables"]["cursor"]]


def in_sequence(*responses: httpx.Response | dict[str, Any] | Exception) -> Responder:
    """Answer successive requests with each response in turn (exceptions are raised)."""
    queue = list(responses)

    def respond(_body: dict[str, Any]) -> httpx.Response | dict[str, Any]:
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return respond


class FakeRepo:
    """Answers FlowLens's issue and PR queries from in-memory nodes, as GitHub would:
    newest-updated first, issues filtered by ``since``, offset cursors."""

    def __init__(self, issues: list[dict[str, Any]], pull_requests: list[dict[str, Any]]):
        self.issues = issues
        self.pull_requests = pull_requests

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        variables = body["variables"]
        if "pullRequests(" in body["query"]:
            connection, nodes = "pullRequests", self.pull_requests
        else:
            connection, nodes = "issues", self.issues
            if since := variables.get("since"):
                nodes = [n for n in nodes if _ts(n["updatedAt"]) >= _ts(since)]
        nodes = sorted(nodes, key=lambda n: _ts(n["updatedAt"]), reverse=True)
        start = int(variables["cursor"] or 0)
        page = nodes[start : start + variables["first"]]
        end = start + len(page)
        return {
            "data": {
                "rateLimit": {
                    "limit": 5000,
                    "cost": 1,
                    "remaining": 4000,
                    "resetAt": "2026-10-29T06:00:00Z",
                },
                "repository": {
                    connection: {
                        "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)},
                        "nodes": page,
                    }
                },
            }
        }


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)
