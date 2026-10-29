"""Record real GitHub API responses into tests/fixtures.

    uv run python scripts/record_fixtures.py

Re-run this only when the queries in flowlens.ingest change. It uses the token from
`gh auth token`, and only response bodies are saved, with email addresses blanked. The
tests themselves never touch the network.

Sources:
- pallets/markupsafe: paginated issue and PR pages (small repo, stable history).
- streamlit/streamlit (the demo repo): issues and PRs picked because each shows a case the
  model must handle, plus releases, deployments, and Actions runs.
- cli/cli: the only manually connected issue and "request changes" review we found.

GitHub reports willCloseTarget as false once the referencing PR is merged, so a finished
issue's closing PR shows up as the closer on its ClosedEvent, not as a closing reference.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from flowlens.ingest.client import API_URL, GRAPHQL_URL, token_from_gh
from flowlens.ingest.delivery import DEPLOYMENTS_QUERY, RELEASES_QUERY
from flowlens.ingest.github import (
    ISSUE_FIELDS_FRAGMENT,
    ISSUES_QUERY,
    PULL_REQUEST_FIELDS_FRAGMENT,
    PULL_REQUESTS_QUERY,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

STREAMLIT_ISSUES = {
    17733: "completed with labels only: never started",
    16726: "assigned, unassigned, reassigned",
    11752: "closed by a PR without being assigned",
    17665: "a PR mentions it without a closing keyword",
    12801: "referenced by a PR in another repo",
    8793: "closed, reopened, closed again",
    8271: "reopened and still open",
    14555: "relabelled from bug to enhancement; PRs only mention it",
    14209: "closed not planned without ever starting",
    10023: "open and assigned",
}
STREAMLIT_PULL_REQUESTS = {
    17730: "merged; bot reviews, a self-review, and a human approval",
    17704: "a bot (github-actions) approves",
    17735: "bot-authored, merged with no human review",
    17670: "closed without merging",
    17688: "merged, closes an issue",
}
CLI_ISSUES = {14614: "manually connected to a PR"}
CLI_PULL_REQUESTS = {11926: "merged after a human requested changes"}


def scrub(value: Any) -> Any:
    """Blank out email addresses (workflow runs carry commit authors'); nothing reads them."""
    if isinstance(value, dict):
        return {
            k: "redacted@example.invalid" if k == "email" else scrub(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def save(name: str, payload: Any) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(scrub(payload), indent=2) + "\n")
    print(f"wrote {path.relative_to(FIXTURES.parent.parent)}")


def graphql(http: httpx.Client, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = http.post(GRAPHQL_URL, json={"query": query, "variables": variables})
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload


def record_pages(
    http: httpx.Client,
    query: str,
    variables: dict[str, Any],
    connection: str,
    prefix: str,
    max_pages: int,
) -> None:
    cursor = None
    for page in range(1, max_pages + 1):
        payload = graphql(http, query, {**variables, "cursor": cursor})
        save(f"{prefix}_page{page}.json", payload)
        page_info = payload["data"]["repository"][connection]["pageInfo"]
        if not page_info["hasNextPage"]:
            return
        cursor = page_info["endCursor"]


def record_items(
    http: httpx.Client, owner: str, name: str, kind: str, numbers: dict[int, str], filename: str
) -> None:
    """One response holding the named issues or PRs, each under an alias like i17733."""
    fragment = ISSUE_FIELDS_FRAGMENT if kind == "issue" else PULL_REQUEST_FIELDS_FRAGMENT
    spread = "IssueFields" if kind == "issue" else "PullRequestFields"
    field = "issue" if kind == "issue" else "pullRequest"
    aliases = "\n".join(
        f"    i{number}: {field}(number: {number}) {{ ...{spread} }}" for number in numbers
    )
    query = (
        "query Items($owner: String!, $name: String!) {\n"
        "  rateLimit { limit cost remaining resetAt }\n"
        f"  repository(owner: $owner, name: $name) {{\n{aliases}\n  }}\n}}\n" + fragment
    )
    save(filename, graphql(http, query, {"owner": owner, "name": name}))


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"bearer {token_from_gh()}", "User-Agent": "flowlens-fixtures"}
    with httpx.Client(headers=headers, timeout=60) as http:
        markupsafe = {"owner": "pallets", "name": "markupsafe"}
        # 8 issues updated since July 2026, three per page: three pages, the last one short.
        record_pages(
            http,
            ISSUES_QUERY,
            {**markupsafe, "first": 3, "since": "2026-07-01T00:00:00Z"},
            "issues",
            "issues",
            max_pages=5,
        )
        # The newest PRs, four per page: two pages, both with more behind them.
        record_pages(
            http,
            PULL_REQUESTS_QUERY,
            {**markupsafe, "first": 4},
            "pullRequests",
            "pull_requests",
            max_pages=2,
        )
        response = http.post(
            GRAPHQL_URL,
            json={
                "query": ISSUES_QUERY,
                "variables": {"owner": "singha105", "name": "no-such-repo-flowlens", "first": 1},
            },
        )
        save("repo_not_found.json", response.json())

        record_items(
            http, "streamlit", "streamlit", "issue", STREAMLIT_ISSUES, "streamlit_issues.json"
        )
        record_items(
            http, "streamlit", "streamlit", "pr", STREAMLIT_PULL_REQUESTS, "streamlit_prs.json"
        )
        record_items(http, "cli", "cli", "issue", CLI_ISSUES, "cli_issues.json")
        record_items(http, "cli", "cli", "pr", CLI_PULL_REQUESTS, "cli_prs.json")

        streamlit = {"owner": "streamlit", "name": "streamlit"}
        record_pages(
            http, RELEASES_QUERY, {**streamlit, "first": 5}, "releases", "releases", max_pages=1
        )
        record_pages(
            http,
            DEPLOYMENTS_QUERY,
            {**streamlit, "first": 40},
            "deployments",
            "deployments",
            max_pages=1,
        )

        rest = {**headers, "Accept": "application/vnd.github+json"}
        workflows = http.get(
            f"{API_URL}/repos/streamlit/streamlit/actions/workflows",
            params={"per_page": 100},
            headers=rest,
        )
        workflows.raise_for_status()
        save("workflows.json", workflows.json())
        build_release = next(
            w for w in workflows.json()["workflows"] if w["name"] == "Build Release"
        )
        url = f"{API_URL}/repos/streamlit/streamlit/actions/workflows/{build_release['id']}/runs"
        params: dict[str, Any] | None = {
            "per_page": 3,
            "created": "2026-06-01T00:00:00Z..2026-10-29T00:00:00Z",
        }
        for page in (1, 2):
            runs = http.get(url, params=params, headers=rest)
            runs.raise_for_status()
            save(f"workflow_runs_page{page}.json", runs.json())
            url, params = runs.links["next"]["url"], None


if __name__ == "__main__":
    main()
