"""Delivery signals: releases, GitHub Deployments, and Actions workflow runs.

Releases and deployments come from GraphQL, newest-created first. Workflow runs are only
available over REST, which caps any filtered search at 1,000 results, so a date range that
holds more is split in half until each part fits.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from flowlens.ingest.client import GitHubClient, GitHubError
from flowlens.ingest.github import format_ts, newer_than, paginate, parse_ts

RELEASE_PAGE_SIZE = 100
DEPLOYMENT_PAGE_SIZE = 100
WORKFLOW_RUN_PAGE_SIZE = 100
RUN_SEARCH_CAP = 1000

RELEASES_QUERY = """
query Releases($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    releases(first: $first, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id tagName name url isDraft isPrerelease createdAt publishedAt
        tagCommit { oid }
        author { login }
      }
    }
  }
}
"""

DEPLOYMENTS_QUERY = """
query Deployments($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    deployments(first: $first, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id environment task createdAt updatedAt commitOid
        ref { name }
        creator { login }
        latestStatus { state createdAt }
        statuses(first: 20) { nodes { id state createdAt } }
      }
    }
  }
}
"""


def fetch_releases(
    client: GitHubClient,
    owner: str,
    name: str,
    since: datetime | None = None,
    page_size: int = RELEASE_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of releases created at or after ``since``."""
    variables = {"owner": owner, "name": name, "first": page_size}
    yield from newer_than(
        paginate(client, RELEASES_QUERY, variables, "releases"), since, "createdAt"
    )


def fetch_deployments(
    client: GitHubClient,
    owner: str,
    name: str,
    since: datetime | None = None,
    page_size: int = DEPLOYMENT_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of deployments (every environment) created at or after ``since``."""
    variables = {"owner": owner, "name": name, "first": page_size}
    pages = paginate(client, DEPLOYMENTS_QUERY, variables, "deployments")
    yield from newer_than(pages, since, "createdAt")


def find_workflow(client: GitHubClient, owner: str, name: str, wanted: str) -> dict[str, Any]:
    """Look a workflow up by its name ("Build Release") or file ("release.yml")."""
    available = []
    for page in client.get_pages(f"/repos/{owner}/{name}/actions/workflows", {"per_page": 100}):
        for workflow in page["workflows"]:
            path = workflow["path"]
            if wanted.lower() in {
                workflow["name"].lower(),
                path.lower(),
                PurePosixPath(path).name.lower(),
            }:
                return workflow
            available.append(workflow["name"])
    raise GitHubError(
        f"No workflow {wanted!r} in {owner}/{name}. Available: {', '.join(sorted(available))}."
    )


def fetch_workflow_runs(
    client: GitHubClient,
    owner: str,
    name: str,
    workflow: dict[str, Any],
    since: datetime | None = None,
    page_size: int = WORKFLOW_RUN_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of a workflow's runs created at or after ``since``, newest first."""
    path = f"/repos/{owner}/{name}/actions/workflows/{workflow['id']}/runs"
    start = since or parse_ts(workflow["created_at"])
    yield from _runs_between(client, path, start, client.now(), page_size)


def _runs_between(
    client: GitHubClient, path: str, start: datetime, end: datetime, page_size: int
) -> Iterator[list[dict[str, Any]]]:
    params = {"per_page": page_size, "created": f"{format_ts(start)}..{format_ts(end)}"}
    pages = client.get_pages(path, params)
    first = next(pages)
    if first["total_count"] > RUN_SEARCH_CAP and end - start > timedelta(hours=1):
        pages.close()
        middle = start + (end - start) / 2
        # Both halves include the midpoint; the duplicate upserts onto itself.
        yield from _runs_between(client, path, middle, end, page_size)
        yield from _runs_between(client, path, start, middle, page_size)
        return
    if first["workflow_runs"]:
        yield first["workflow_runs"]
    for page in pages:
        if page["workflow_runs"]:
            yield page["workflow_runs"]


def release_row(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "tag_name": node["tagName"],
        "name": node.get("name"),
        "url": node.get("url"),
        "is_draft": node.get("isDraft"),
        "is_prerelease": node.get("isPrerelease"),
        "created_at": parse_ts(node["createdAt"]),
        "published_at": parse_ts(node.get("publishedAt")),
        "target_sha": (node.get("tagCommit") or {}).get("oid"),
        "author": (node.get("author") or {}).get("login"),
        "raw": node,
    }


def deployment_row(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "environment": node.get("environment"),
        "task": node.get("task"),
        "ref_name": (node.get("ref") or {}).get("name"),
        "sha": node.get("commitOid"),
        "creator": (node.get("creator") or {}).get("login"),
        "created_at": parse_ts(node["createdAt"]),
        "updated_at": parse_ts(node.get("updatedAt") or node["createdAt"]),
        "latest_state": (node.get("latestStatus") or {}).get("state"),
        "raw": node,
    }


def deployment_status_rows(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": status["id"],
            "deployment_id": node["id"],
            "state": status["state"],
            "created_at": parse_ts(status["createdAt"]),
        }
        for status in (node.get("statuses") or {}).get("nodes") or []
        if status
    ]


def workflow_run_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["id"],
        "workflow_id": run.get("workflow_id"),
        "workflow_name": run.get("name"),
        "workflow_path": run.get("path"),
        "display_title": run.get("display_title"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "head_sha": run.get("head_sha"),
        "head_branch": run.get("head_branch"),
        "run_attempt": run.get("run_attempt"),
        "created_at": parse_ts(run["created_at"]),
        "updated_at": parse_ts(run["updated_at"]),
        "run_started_at": parse_ts(run.get("run_started_at")),
        "url": run.get("html_url"),
    }
