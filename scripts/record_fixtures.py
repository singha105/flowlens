"""Record real GraphQL responses into tests/fixtures.

    uv run python scripts/record_fixtures.py

Re-run this only when the queries in flowlens.ingest.github change. It uses the token from
`gh auth token`, and only response bodies are saved. The tests themselves never touch the
network.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from flowlens.ingest.github import GRAPHQL_URL, ISSUES_QUERY, PULL_REQUESTS_QUERY, token_from_gh

OWNER, NAME = "pallets", "markupsafe"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def record(
    http: httpx.Client,
    query: str,
    variables: dict[str, Any],
    connection: str,
    prefix: str,
    max_pages: int,
) -> None:
    cursor = None
    for page in range(1, max_pages + 1):
        response = http.post(
            GRAPHQL_URL, json={"query": query, "variables": {**variables, "cursor": cursor}}
        )
        response.raise_for_status()
        payload = response.json()
        path = FIXTURES / f"{prefix}_page{page}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {path.relative_to(FIXTURES.parent.parent)}")
        page_info = payload["data"]["repository"][connection]["pageInfo"]
        if not page_info["hasNextPage"]:
            return
        cursor = page_info["endCursor"]


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"bearer {token_from_gh()}", "User-Agent": "flowlens-fixtures"}
    with httpx.Client(headers=headers, timeout=60) as http:
        # 8 issues updated since July 2026, three per page: three pages, the last one short.
        record(
            http,
            ISSUES_QUERY,
            {"owner": OWNER, "name": NAME, "first": 3, "since": "2026-07-01T00:00:00Z"},
            "issues",
            "issues",
            max_pages=5,
        )
        # The newest PRs, four per page: two pages, both with more behind them.
        record(
            http,
            PULL_REQUESTS_QUERY,
            {"owner": OWNER, "name": NAME, "first": 4},
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
        path = FIXTURES / "repo_not_found.json"
        path.write_text(json.dumps(response.json(), indent=2) + "\n")
        print(f"wrote {path.relative_to(FIXTURES.parent.parent)}")


if __name__ == "__main__":
    main()
