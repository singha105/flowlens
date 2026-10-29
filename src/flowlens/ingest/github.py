"""Issues and pull requests over GraphQL: queries, pagination, and row mapping.

Issues and pull requests are both read newest-updated first. For issues GitHub filters by
``since`` server-side; pull requests have no such filter, so we stop paging at the first PR
older than ``since``. Updating an item during a crawl only moves it to the front of the
ordering, so a crawl can repeat items but never skips one that hasn't changed.

Nested lists (an issue's timeline, a PR's reviews) come back capped on the first page; the
rest is fetched with follow-up ``node(id:)`` queries before the item is yielded.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from flowlens.ingest.client import GitHubClient, GitHubError

# GitHub silently drops timeline events from big issue pages. At 50 issues a page, 52 of
# 100 streamlit issues came back missing events, with no error and hasNextPage false
# (totalCount drops with them). Pages of 10 and 25 matched per-issue fetches on 300 issues.
ISSUE_PAGE_SIZE = 25
# PR pages of 50 matched per-PR fetches (reviews, first commit, closing references).
PULL_REQUEST_PAGE_SIZE = 50

ISSUE_EVENT_TYPES = (
    "[ASSIGNED_EVENT, UNASSIGNED_EVENT, LABELED_EVENT, UNLABELED_EVENT, "
    "CROSS_REFERENCED_EVENT, CONNECTED_EVENT, CLOSED_EVENT, REOPENED_EVENT]"
)

# Each query is composed from exactly the fragments it spreads: GraphQL rejects a
# document that defines a fragment it doesn't use.
LINKED_PR_FRAGMENT = """
fragment LinkedPullRequest on PullRequest { number createdAt repository { nameWithOwner } }
"""

TIMELINE_FRAGMENT = (
    """
fragment IssueTimelineItem on IssueTimelineItems {
  __typename
  ... on AssignedEvent {
    id createdAt actor { login }
    assignee { ... on User { login } ... on Bot { login } ... on Mannequin { login } }
  }
  ... on UnassignedEvent {
    id createdAt actor { login }
    assignee { ... on User { login } ... on Bot { login } ... on Mannequin { login } }
  }
  ... on LabeledEvent { id createdAt actor { login } label { name } }
  ... on UnlabeledEvent { id createdAt actor { login } label { name } }
  ... on CrossReferencedEvent {
    id createdAt willCloseTarget actor { login }
    source { __typename ...LinkedPullRequest }
  }
  ... on ConnectedEvent { id createdAt actor { login } subject { __typename ...LinkedPullRequest } }
  ... on ClosedEvent {
    id createdAt stateReason actor { login }
    closer { __typename ...LinkedPullRequest }
  }
  ... on ReopenedEvent { id createdAt actor { login } }
}
"""
    + LINKED_PR_FRAGMENT
)

ISSUE_FIELDS_FRAGMENT = (
    """
fragment IssueFields on Issue {
  id number title url state stateReason createdAt updatedAt closedAt
  author { login __typename }
  labels(first: 50) { nodes { name } }
  assignees(first: 20) { nodes { login } }
  timelineItems(first: 50, itemTypes: """
    + ISSUE_EVENT_TYPES
    + """) {
    pageInfo { hasNextPage endCursor }
    nodes { ...IssueTimelineItem }
  }
}
"""
    + TIMELINE_FRAGMENT
)

REVIEW_FRAGMENT = """
fragment Review on PullRequestReview { id state submittedAt author { login __typename } }
"""

PULL_REQUEST_FIELDS_FRAGMENT = (
    """
fragment PullRequestFields on PullRequest {
  id number title url state isDraft createdAt updatedAt mergedAt closedAt
  additions deletions baseRefName
  mergeCommit { oid }
  author { login __typename }
  labels(first: 50) { nodes { name } }
  commits(first: 1) { nodes { commit { authoredDate committedDate } } }
  closingIssuesReferences(first: 10) { nodes { number repository { nameWithOwner } } }
  reviews(first: 50) { pageInfo { hasNextPage endCursor } nodes { ...Review } }
}
"""
    + REVIEW_FRAGMENT
)

ISSUES_QUERY = (
    """
query Issues($owner: String!, $name: String!, $first: Int!, $cursor: String, $since: DateTime) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issues(first: $first, after: $cursor, filterBy: {since: $since},
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...IssueFields }
    }
  }
}
"""
    + ISSUE_FIELDS_FRAGMENT
)

PULL_REQUESTS_QUERY = (
    """
query PullRequests($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...PullRequestFields }
    }
  }
}
"""
    + PULL_REQUEST_FIELDS_FRAGMENT
)

ISSUE_TIMELINE_QUERY = (
    """
query IssueTimeline($id: ID!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  node(id: $id) {
    ... on Issue {
      timelineItems(first: 100, after: $cursor, itemTypes: """
    + ISSUE_EVENT_TYPES
    + """) {
        pageInfo { hasNextPage endCursor }
        nodes { ...IssueTimelineItem }
      }
    }
  }
}
"""
    + TIMELINE_FRAGMENT
)

PULL_REQUEST_REVIEWS_QUERY = (
    """
query PullRequestReviews($id: ID!, $cursor: String) {
  rateLimit { limit cost remaining resetAt }
  node(id: $id) {
    ... on PullRequest {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ...Review }
      }
    }
  }
}
"""
    + REVIEW_FRAGMENT
)

TIMELINE_EVENT_TYPES = {
    "AssignedEvent": "assigned",
    "UnassignedEvent": "unassigned",
    "LabeledEvent": "labeled",
    "UnlabeledEvent": "unlabeled",
    "CrossReferencedEvent": "cross_referenced",
    "ConnectedEvent": "connected",
    "ClosedEvent": "closed",
    "ReopenedEvent": "reopened",
}


def paginate(
    client: GitHubClient, query: str, variables: dict[str, Any], connection: str
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


def newer_than(
    pages: Iterator[list[dict[str, Any]]], since: datetime | None, field: str
) -> Iterator[list[dict[str, Any]]]:
    """Trim a newest-first stream of pages to nodes whose ``field`` is at or after ``since``.

    The first older node ends the stream, so no further pages are requested.
    """
    for nodes in pages:
        if since is None:
            yield nodes
            continue
        fresh = [node for node in nodes if parse_ts(node[field]) >= since]
        if fresh:
            yield fresh
        if len(fresh) < len(nodes):
            return


def complete_connection(client: GitHubClient, node: dict[str, Any], field: str, query: str) -> int:
    """Fetch the rest of ``node[field]`` in place if GitHub capped it. Returns pages added."""
    connection = node.get(field)
    pages = 0
    while connection and connection["pageInfo"]["hasNextPage"]:
        data = client.query(
            query, {"id": node["id"], "cursor": connection["pageInfo"]["endCursor"]}
        )
        more = data["node"][field]
        connection["nodes"].extend(more["nodes"])
        connection["pageInfo"] = more["pageInfo"]
        pages += 1
    return pages


def fetch_issues(
    client: GitHubClient,
    owner: str,
    name: str,
    since: datetime | None = None,
    page_size: int = ISSUE_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of issues updated at or after ``since`` (all issues when None),
    each with its complete timeline."""
    variables = {"owner": owner, "name": name, "first": page_size, "since": format_ts(since)}
    for nodes in paginate(client, ISSUES_QUERY, variables, "issues"):
        for node in nodes:
            complete_issue(client, node)
        yield nodes


def complete_issue(client: GitHubClient, node: dict[str, Any]) -> None:
    """Fetch the rest of an issue's timeline in place, re-reading it if it was cut short."""
    complete_connection(client, node, "timelineItems", ISSUE_TIMELINE_QUERY)
    if node["state"] == "CLOSED" and not _has_event(node, "ClosedEvent"):
        # Every closed issue has a close event, so this timeline lost events (see
        # ISSUE_PAGE_SIZE). Read it again on its own.
        node["timelineItems"] = {"pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}
        complete_connection(client, node, "timelineItems", ISSUE_TIMELINE_QUERY)


def fetch_pull_requests(
    client: GitHubClient,
    owner: str,
    name: str,
    since: datetime | None = None,
    page_size: int = PULL_REQUEST_PAGE_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of PRs updated at or after ``since``, each with all of its reviews."""
    variables = {"owner": owner, "name": name, "first": page_size}
    pages = paginate(client, PULL_REQUESTS_QUERY, variables, "pullRequests")
    for nodes in newer_than(pages, since, "updatedAt"):
        for node in nodes:
            complete_connection(client, node, "reviews", PULL_REQUEST_REVIEWS_QUERY)
        yield nodes


def issue_row(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten an issue node into an ``issues`` row. ``raw`` keeps the node as fetched,
    minus the timeline, which goes to ``issue_events``."""
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
        "raw": {key: value for key, value in node.items() if key != "timelineItems"},
    }


def issue_event_rows(node: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``issue_events`` row per timeline event we track.

    Cross-references from issues (rather than PRs) say nothing about work on this issue,
    so they're dropped. PR links keep the PR's repository; the model decides whether a PR
    in another repo counts.
    """
    rows = []
    for event in _nodes(node.get("timelineItems")):
        event_type = TIMELINE_EVENT_TYPES.get(event.get("__typename", ""))
        if event_type is None or "id" not in event:
            continue
        linked = event.get("source") or event.get("subject") or event.get("closer") or {}
        is_pr = linked.get("__typename") == "PullRequest"
        if event_type == "cross_referenced" and not is_pr:
            continue
        rows.append(
            {
                "id": event["id"],
                "issue_number": node["number"],
                "event_type": event_type,
                "created_at": parse_ts(event["createdAt"]),
                "actor": _login(event.get("actor")),
                "label": (event.get("label") or {}).get("name"),
                "assignee": _login(event.get("assignee")),
                "pr_number": linked.get("number") if is_pr else None,
                "pr_repo": (linked.get("repository") or {}).get("nameWithOwner") if is_pr else None,
                "pr_created_at": parse_ts(linked.get("createdAt")) if is_pr else None,
                "will_close": event.get("willCloseTarget"),
                "state_reason": event.get("stateReason"),
                "raw": event,
            }
        )
    return rows


def pull_request_row(node: dict[str, Any], repo: str) -> dict[str, Any]:
    """Flatten a PR node into a ``pull_requests`` row. ``raw`` keeps the node as fetched,
    minus its reviews, which go to ``pr_reviews``."""
    author = _login(node.get("author"))
    commits = _nodes(node.get("commits"))
    reviews = _nodes(node.get("reviews"))
    first_review_at, first_reviewer = first_review(reviews, author)
    first_approval_at, _ = first_review(reviews, author, states={"APPROVED"})
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
        "merge_commit_sha": (node.get("mergeCommit") or {}).get("oid"),
        "first_commit_at": parse_ts(commits[0]["commit"]["authoredDate"]) if commits else None,
        "first_review_at": first_review_at,
        "first_reviewer": first_reviewer,
        "first_approval_at": first_approval_at,
        "review_count": sum(1 for review in reviews if _counts_as_review(review, author)),
        "closing_issues": [
            issue["number"]
            for issue in _nodes(node.get("closingIssuesReferences"))
            if _same_repo((issue.get("repository") or {}).get("nameWithOwner"), repo)
        ],
        "labels": [label["name"] for label in _nodes(node.get("labels"))],
        "url": node["url"],
        "raw": {key: value for key, value in node.items() if key != "reviews"},
    }


def review_rows(node: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``pr_reviews`` row per review on a PR node, pending ones included."""
    return [
        {
            "id": review["id"],
            "pr_number": node["number"],
            "author": _login(review.get("author")),
            "author_type": _actor_type(review.get("author")),
            "state": review["state"],
            "submitted_at": parse_ts(review.get("submittedAt")),
        }
        for review in _nodes(node.get("reviews"))
        if review.get("id")
    ]


def first_review(
    reviews: list[dict[str, Any]], pr_author: str | None, states: set[str] | None = None
) -> tuple[datetime | None, str | None]:
    """Earliest submitted review by a person other than the PR author.

    Replying to a review thread on your own PR creates a review authored by you, and bots
    (Copilot, AI reviewers) review within minutes of a PR opening, so neither counts as the
    first review a human waited for. Pending reviews have no timestamp. ``states`` limits
    the search, e.g. to ``{"APPROVED"}`` for the first approval.
    """
    earliest: tuple[datetime, str | None] | None = None
    for review in reviews:
        if not _counts_as_review(review, pr_author):
            continue
        if states is not None and review.get("state") not in states:
            continue
        submitted = parse_ts(review["submittedAt"])
        if earliest is None or submitted < earliest[0]:
            earliest = (submitted, _login(review.get("author")))
    return earliest if earliest else (None, None)


def _counts_as_review(review: dict[str, Any], pr_author: str | None) -> bool:
    if review.get("submittedAt") is None:
        return False
    reviewer = review.get("author")
    if _actor_type(reviewer) == "Bot":
        return False
    return pr_author is None or _login(reviewer) != pr_author


def parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime."""
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def format_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_event(node: dict[str, Any], typename: str) -> bool:
    return any(event.get("__typename") == typename for event in _nodes(node.get("timelineItems")))


def _same_repo(name_with_owner: str | None, repo: str) -> bool:
    return name_with_owner is not None and name_with_owner.lower() == repo.lower()


def _nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not connection:
        return []
    return [node for node in connection.get("nodes") or [] if node]


def _login(actor: dict[str, Any] | None) -> str | None:
    # A deleted account comes back as a null actor (GitHub shows it as "ghost").
    return actor.get("login") if actor else None


def _actor_type(actor: dict[str, Any] | None) -> str | None:
    return actor.get("__typename") if actor else None
