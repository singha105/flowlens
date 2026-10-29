"""Compare candidate demo repos on the last 12 months, using FlowLens's own rules.

    uv run python scripts/compare_repos.py cli/cli streamlit/streamlit pydantic/pydantic

Volumes come from GitHub search. Coverage shares come from a sample of each repo's 100
most recently updated closed issues and merged PRs, fetched the way `flowlens ingest`
fetches them and run through the same model with the default flowlens.yaml settings.
The output is the table in docs/decisions/0001-demo-repo.md.
"""

import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from flowlens.config import Config
from flowlens.ingest.client import GitHubClient, token_from_gh
from flowlens.ingest.github import (
    ISSUE_FIELDS_FRAGMENT,
    ISSUE_PAGE_SIZE,
    PULL_REQUEST_FIELDS_FRAGMENT,
    PULL_REQUEST_PAGE_SIZE,
    PULL_REQUEST_REVIEWS_QUERY,
    complete_connection,
    complete_issue,
    issue_event_rows,
    issue_row,
    paginate,
    parse_ts,
    pull_request_row,
)
from flowlens.model.work_items import issue_work_item

SAMPLE = 100

COUNTS_QUERY = """
query Counts($closed: String!, $completed: String!, $merged: String!) {
  rateLimit { limit cost remaining resetAt }
  closed: search(type: ISSUE, query: $closed, first: 0) { issueCount }
  completed: search(type: ISSUE, query: $completed, first: 0) { issueCount }
  merged: search(type: ISSUE, query: $merged, first: 0) { issueCount }
}
"""

ISSUES_SAMPLE_QUERY = (
    """
query Sample($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issues(first: $first, after: $cursor, states: CLOSED,
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...IssueFields }
    }
  }
}
"""
    + ISSUE_FIELDS_FRAGMENT
)

PRS_SAMPLE_QUERY = (
    """
query Sample($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, after: $cursor, states: MERGED,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...PullRequestFields }
    }
  }
}
"""
    + PULL_REQUEST_FIELDS_FRAGMENT
)

DELIVERY_QUERY = """
query Delivery($owner: String!, $name: String!) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    releases(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { isDraft isPrerelease publishedAt }
    }
    deployments(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { environment }
    }
  }
}
"""


def sample(client: GitHubClient, query: str, variables: dict, connection: str, page_size: int):
    """The first SAMPLE nodes, paged at the size FlowLens ingests with."""
    nodes: list[dict] = []
    for page in paginate(client, query, {**variables, "first": page_size}, connection):
        nodes.extend(page)
        if len(nodes) >= SAMPLE:
            break
    return nodes[:SAMPLE]


def share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "n/a"


def compare(client: GitHubClient, repo: str, since: datetime) -> dict[str, str]:
    owner, name = repo.split("/")
    day = f"{since:%Y-%m-%d}"
    counts = client.query(
        COUNTS_QUERY,
        {
            "closed": f"repo:{repo} is:issue is:closed closed:>={day}",
            "completed": f"repo:{repo} is:issue is:closed reason:completed closed:>={day}",
            "merged": f"repo:{repo} is:pr is:merged merged:>={day}",
        },
    )
    variables = {"owner": owner, "name": name}
    issues = sample(client, ISSUES_SAMPLE_QUERY, variables, "issues", ISSUE_PAGE_SIZE)
    for node in issues:
        complete_issue(client, node)
    pr_nodes = sample(client, PRS_SAMPLE_QUERY, variables, "pullRequests", PULL_REQUEST_PAGE_SIZE)
    for node in pr_nodes:
        complete_connection(client, node, "reviews", PULL_REQUEST_REVIEWS_QUERY)
    delivery = client.query(DELIVERY_QUERY, variables)["repository"]

    config = Config()
    items = [
        issue_work_item(issue_row(node), issue_event_rows(node), config, repo=repo)[0]
        for node in issues
    ]
    finished = [item for item in items if item.outcome in {"completed", "closed_no_reason"}]
    signals = Counter(item.started_signal for item in finished if item.started_at)
    closed_events = [
        e for node in issues for e in issue_event_rows(node) if e["event_type"] == "closed"
    ]

    prs = [pull_request_row(node, repo) for node in pr_nodes]
    humans = [pr for pr in prs if pr["author_type"] != "Bot"]
    releases = [
        r
        for r in delivery["releases"]["nodes"]
        if r["publishedAt"] and not r["isDraft"] and not r["isPrerelease"]
        and parse_ts(r["publishedAt"]) >= since
    ]  # fmt: skip
    environments = Counter(d["environment"] for d in delivery["deployments"]["nodes"])

    closed, completed, merged = (counts[k]["issueCount"] for k in ("closed", "completed", "merged"))
    reviewed = sum(pr["review_count"] > 0 for pr in humans)
    approved = sum(pr["first_approval_at"] is not None for pr in humans)
    not_planned = sum(item.outcome == "not_planned" for item in items)
    with_reason = sum(bool(event["state_reason"]) for event in closed_events)
    return {
        "Issues closed": f"{closed:,}",
        "of which completed": f"{completed:,}",
        "PRs merged": f"{merged:,}",
        "Closed + merged": f"{closed + merged:,}",
        "Stable releases": f"{len(releases)}{'+' if len(releases) == 100 else ''}",
        "Deployments (latest 100)": ", ".join(f"{env} {n}" for env, n in environments.most_common())
        or "none",
        "Merged PRs from bots": share(len(prs) - len(humans), len(prs)),
        "Human PRs with a human review": share(reviewed, len(humans)),
        "Human PRs approved": share(approved, len(humans)),
        "Closed issues not planned": share(not_planned, len(items)),
        "Finished issues with a start": share(sum(signals.values()), len(finished)),
        "... by assignment": share(signals["first_assigned"], len(finished)),
        "... by linked PR": share(signals["first_linked_pr"], len(finished)),
        "Close events with a reason": share(with_reason, len(closed_events)),
    }


def main(repos: list[str]) -> None:
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=365
    )
    with GitHubClient(token_from_gh()) as client:
        results = {repo: compare(client, repo, since) for repo in repos}
        print(f"Since {since:%Y-%m-%d}; samples of {SAMPLE} closed issues and merged PRs.\n")
        print("| | " + " | ".join(repos) + " |")
        print("|---|" + "---:|" * len(repos))
        for row in results[repos[0]]:
            print(f"| {row} | " + " | ".join(results[repo][row] for repo in repos) + " |")
        rate = client.rate_limit
        print(
            f"\n{client.calls} API calls, GraphQL cost {client.cost}, {rate.remaining} points left"
        )


if __name__ == "__main__":
    main(sys.argv[1:] or ["cli/cli", "streamlit/streamlit", "pydantic/pydantic"])
