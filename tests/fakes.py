"""Offline stand-ins for the GitHub APIs, built on recorded fixtures."""

import copy
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from flowlens.ingest import delivery, github
from flowlens.ingest.client import API_URL, GitHubClient
from flowlens.ingest.runner import FETCH_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)
RATE_LIMIT = {"limit": 5000, "cost": 1, "remaining": 4000, "resetAt": "2026-10-29T06:00:00Z"}

Result = httpx.Response | dict[str, Any]
Responder = Callable[[dict[str, Any]], Result]
RestResponder = Callable[[str, dict[str, str]], Result]


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def fixture_nodes(prefix: str, pages: int) -> list[dict[str, Any]]:
    """Nodes from recorded <prefix>_pageN.json files, in order."""
    nodes = []
    for page in range(1, pages + 1):
        repository = load_fixture(f"{prefix}_page{page}.json")["data"]["repository"]
        nodes.extend(next(iter(repository.values()))["nodes"])
    return nodes


def fixture_items(name: str) -> dict[int, dict[str, Any]]:
    """Issues or PRs from a recorded alias query (streamlit_issues.json ...), by number."""
    repository = load_fixture(name)["data"]["repository"]
    return {node["number"]: node for node in repository.values()}


class FakeGitHub:
    """Stands in for api.github.com: records each request and answers it.

    GraphQL requests go to ``responder`` with the parsed body; REST GETs go to ``rest``
    with the path and query parameters.
    """

    def __init__(self, responder: Responder | None = None, rest: RestResponder | None = None):
        self.responder = responder
        self.rest = rest
        self.requests: list[dict[str, Any]] = []
        self.rest_requests: list[tuple[str, dict[str, str]]] = []
        self.sleeps: list[float] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            self.requests.append(body)
            assert self.responder is not None, "unexpected GraphQL request"
            result = self.responder(body)
        else:
            params = dict(request.url.params)
            self.rest_requests.append((request.url.path, params))
            assert self.rest is not None, f"unexpected REST request {request.url.path}"
            result = self.rest(request.url.path, params)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    def client(self, **options: Any) -> GitHubClient:
        return GitHubClient(
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


def in_sequence(*responses: Result | Exception) -> Callable[..., Result]:
    """Answer successive requests with each response in turn (exceptions are raised)."""
    queue = list(responses)

    def respond(*_args: Any) -> Result:
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return respond


class FakeRepo:
    """Answers FlowLens's queries from in-memory data, the way GitHub would.

    GraphQL connections come back newest first (issues filtered by ``since``) with offset
    cursors; ``cap`` truncates a nested connection so the follow-up ``node(id:)`` queries
    get exercised. REST serves workflows and their runs, honouring ``created`` ranges and
    paging with ``Link`` headers.
    """

    def __init__(
        self,
        issues: list[dict[str, Any]] = (),
        pull_requests: list[dict[str, Any]] = (),
        *,
        releases: list[dict[str, Any]] = (),
        deployments: list[dict[str, Any]] = (),
        workflows: list[dict[str, Any]] = (),
        runs: list[dict[str, Any]] = (),
    ):
        self.issues = list(issues)
        self.pull_requests = list(pull_requests)
        self.releases = list(releases)
        self.deployments = list(deployments)
        self.workflows = list(workflows)
        self.runs = list(runs)
        self._full: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def cap(self, node: dict[str, Any], field: str, keep: int, silently: bool = False):
        """Return a copy of ``node`` whose ``field`` holds only ``keep`` items for now.

        GitHub normally says there's more (``hasNextPage``); ``silently`` drops the rest
        without saying so, as it does on big issue pages.
        """
        full = node[field]["nodes"]
        self._full[(node["id"], field)] = full
        capped = copy.deepcopy(node)
        more = len(full) > keep and not silently
        capped[field] = {
            "pageInfo": {"hasNextPage": more, "endCursor": str(keep)},
            "nodes": copy.deepcopy(full[:keep]),
        }
        return capped

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        query, variables = body["query"], body["variables"]
        if "node(id:" in query:
            field = "timelineItems" if "timelineItems(" in query else "reviews"
            items = self._full[(variables["id"], field)]
            return _data({"node": {field: _page(items, variables["cursor"], 100)}})
        if "pullRequests(" in query:
            connection, nodes, order = "pullRequests", self.pull_requests, "updatedAt"
        elif "releases(" in query:
            connection, nodes, order = "releases", self.releases, "createdAt"
        elif "deployments(" in query:
            connection, nodes, order = "deployments", self.deployments, "createdAt"
        else:
            connection, nodes, order = "issues", self.issues, "updatedAt"
            if since := variables.get("since"):
                nodes = [n for n in nodes if _ts(n["updatedAt"]) >= _ts(since)]
        nodes = sorted(nodes, key=lambda n: _ts(n[order]), reverse=True)
        page = _page(nodes, variables["cursor"], variables["first"])
        return _data({"repository": {connection: page}})

    def rest(self, path: str, params: dict[str, str]) -> Result:
        if path.endswith("/actions/workflows"):
            return {"total_count": len(self.workflows), "workflows": self.workflows}
        match = re.search(r"/actions/workflows/(\d+)/runs$", path)
        assert match, path
        runs = [run for run in self.runs if run["workflow_id"] == int(match[1])]
        start, _, end = params["created"].partition("..")
        runs = [run for run in runs if _ts(start) <= _ts(run["created_at"]) <= _ts(end)]
        runs.sort(key=lambda run: run["created_at"], reverse=True)
        per_page, page = int(params["per_page"]), int(params.get("page", 1))
        chunk = runs[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(runs):
            query = urlencode({**params, "page": page + 1})
            headers["Link"] = f'<{API_URL}{path}?{query}>; rel="next"'
        return httpx.Response(
            200, json={"total_count": len(runs), "workflow_runs": chunk}, headers=headers
        )


def _page(items: list[dict[str, Any]], cursor: str | None, first: int) -> dict[str, Any]:
    start = int(cursor or 0)
    chunk = items[start : start + first]
    end = start + len(chunk)
    return {"pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end)}, "nodes": chunk}


def _data(data: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"rateLimit": RATE_LIMIT, **data}}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fill_with_streamlit(store: Any, *, coverage_from: datetime, finished_at: datetime) -> None:
    """Load every recorded streamlit fixture into ``store``, as one successful ingest run."""
    repo = "streamlit/streamlit"
    issues = list(fixture_items("streamlit_issues.json").values())
    prs = list(fixture_items("streamlit_prs.json").values())
    deployments = load_fixture("deployments_page1.json")["data"]["repository"]["deployments"]
    releases = load_fixture("releases_page1.json")["data"]["repository"]["releases"]
    runs = [
        run
        for page in (1, 2)
        for run in load_fixture(f"workflow_runs_page{page}.json")["workflow_runs"]
    ]
    store.upsert("issues", [github.issue_row(node) for node in issues])
    store.upsert("issue_events", [row for node in issues for row in github.issue_event_rows(node)])
    store.upsert("pull_requests", [github.pull_request_row(node, repo) for node in prs])
    store.upsert("pr_reviews", [row for node in prs for row in github.review_rows(node)])
    store.upsert("releases", [delivery.release_row(node) for node in releases["nodes"]])
    store.upsert("deployments", [delivery.deployment_row(node) for node in deployments["nodes"]])
    store.upsert(
        "deployment_statuses",
        [row for node in deployments["nodes"] for row in delivery.deployment_status_rows(node)],
    )
    store.upsert("workflow_runs", [delivery.workflow_run_row(run) for run in runs])
    run_id = store.start_run(
        repo,
        mode="full",
        fetch_version=FETCH_VERSION,
        since_requested=coverage_from,
        coverage_from=coverage_from,
    )
    store.finish_run(run_id, status="success", api_calls=1, api_cost=1)
    store.con.execute(
        "UPDATE ingest_runs SET finished_at = ? WHERE run_id = ?",
        [finished_at.astimezone(UTC).replace(tzinfo=None), run_id],
    )
