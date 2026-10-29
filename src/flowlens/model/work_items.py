"""Canonical work items: issues and PRs on one footing, with derived start and finish.

Every function here is pure. It takes rows shaped the way flowlens.storage returns them
(dicts with aware UTC datetimes) and returns new values, so each rule can be tested alone.

How an issue's start is decided (``started_signal`` in flowlens.yaml):

- ``first_assigned``: the first time anyone was assigned.
- ``first_linked_pr``: when the first linked PR in this repo was opened. "Linked" follows
  GitHub's Development sidebar: a closing keyword ("fixes #12"), a manual connection, the
  PR that closed the issue, or the PR listing the issue in its closing references. A plain
  mention doesn't count; it's no claim that anyone is working on the issue.
- ``label:<name>``: the first time that label was added (case-insensitive).

The earliest signal wins. Signals after the item was finished are ignored (triage after
the fact), and an issue never starts before it was opened. An issue with no signal before
it closed has no start: it was closed without ever being worked on, as far as GitHub knows.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flowlens.config import LABEL_KINDS, Config

Row = dict[str, Any]

ISSUE_OUTCOMES = {"COMPLETED": "completed", "NOT_PLANNED": "not_planned", "DUPLICATE": "duplicate"}


@dataclass(frozen=True)
class WorkItem:
    id: str
    type: str  # issue | pr
    number: int
    title: str | None
    url: str | None
    author: str | None
    author_type: str | None
    created_at: datetime
    started_at: datetime | None
    started_signal: str | None
    done_at: datetime | None
    outcome: str
    labels: list[str]
    kinds: list[str]
    linked: list[int]
    reopen_count: int
    excluded: bool
    exclude_reason: str | None


@dataclass(frozen=True)
class WorkItemEvent:
    work_item_id: str
    occurred_at: datetime
    kind: str
    actor: str | None = None
    detail: str | None = None
    source_id: str | None = None


def derive_start(
    candidates: Iterable[tuple[str, datetime | None]],
    *,
    done_at: datetime | None,
    floor: datetime | None = None,
) -> tuple[datetime | None, str | None]:
    """Earliest (signal, time) candidate that fired by ``done_at``, clamped to ``floor``.

    Ties go to the candidate listed first, so config order breaks them.
    """
    best: tuple[datetime, str] | None = None
    for name, at in candidates:
        if at is None or (done_at is not None and at > done_at):
            continue
        if best is None or at < best[0]:
            best = (at, name)
    if best is None:
        return None, None
    at, name = best
    if floor is not None and at < floor:
        at = floor
    return at, name


def classify(labels: Iterable[str], config: Config) -> list[str]:
    """Which of bug / incident / hotfix the labels mark an item as."""
    present = {label.lower() for label in labels}
    return [
        kind
        for kind in LABEL_KINDS
        if present & {name.lower() for name in config.labels.get(kind, ())}
    ]


def exclusion_reason(
    *, state_reason: str | None, labels: Iterable[str], author_type: str | None, config: Config
) -> str | None:
    """Why an item is left out of metrics, or None if it counts."""
    rules = config.exclude
    if state_reason and state_reason.upper() in rules.state_reasons:
        return f"state_reason:{state_reason.upper()}"
    present = {label.lower() for label in labels}
    for label in rules.labels:
        if label.lower() in present:
            return f"label:{label}"
    if rules.bots and author_type == "Bot":
        return "bot"
    return None


def linked_prs(
    events: Iterable[Row], repo: str, closing_prs: dict[int, datetime] | None = None
) -> dict[int, tuple[datetime, Row | None]]:
    """Same-repo PRs linked to an issue: number -> (PR opened at, linking event)."""
    links: dict[int, tuple[datetime, Row | None]] = {}
    for event in events:
        number, opened = event.get("pr_number"), event.get("pr_created_at")
        if number is None or opened is None:
            continue
        if (event.get("pr_repo") or "").lower() != repo.lower():
            continue
        is_link = event["event_type"] in {"connected", "closed"} or (
            event["event_type"] == "cross_referenced" and event.get("will_close")
        )
        if is_link and number not in links:
            links[number] = (opened, event)
    for number, opened in (closing_prs or {}).items():
        links.setdefault(number, (opened, None))
    return links


def issue_work_item(
    issue: Row,
    events: list[Row],
    config: Config,
    *,
    repo: str,
    closing_prs: dict[int, datetime] | None = None,
) -> tuple[WorkItem, list[WorkItemEvent]]:
    """Turn an ``issues`` row and its ``issue_events`` into a work item and its events.

    ``closing_prs`` maps PRs that list this issue in their closing references to when they
    were opened; it catches links the issue's own timeline missed.
    """
    item_id = f"issue#{issue['number']}"
    events = sorted(events, key=lambda event: event["created_at"])
    links = linked_prs(events, repo, closing_prs)

    if issue["state"] == "CLOSED":
        done_at = issue["closed_at"]
        outcome = ISSUE_OUTCOMES.get((issue.get("state_reason") or "").upper(), "closed_no_reason")
    else:
        done_at, outcome = None, "open"

    first_assigned = next((e["created_at"] for e in events if e["event_type"] == "assigned"), None)
    first_labeled: dict[str, datetime] = {}
    for event in events:
        if event["event_type"] == "labeled" and event.get("label"):
            first_labeled.setdefault(event["label"].lower(), event["created_at"])
    first_link = min((opened for opened, _ in links.values()), default=None)

    candidates: list[tuple[str, datetime | None]] = []
    for signal in config.started_signal:
        if signal == "first_assigned":
            candidates.append((signal, first_assigned))
        elif signal == "first_linked_pr":
            candidates.append((signal, first_link))
        else:
            candidates.append((signal, first_labeled.get(signal[6:].strip().lower())))
    started_at, started_signal = derive_start(
        candidates, done_at=done_at, floor=issue["created_at"]
    )

    reason = exclusion_reason(
        state_reason=issue.get("state_reason") if issue["state"] == "CLOSED" else None,
        labels=issue["labels"],
        author_type=issue.get("author_type"),
        config=config,
    )
    item = WorkItem(
        id=item_id,
        type="issue",
        number=issue["number"],
        title=issue.get("title"),
        url=issue.get("url"),
        author=issue.get("author"),
        author_type=issue.get("author_type"),
        created_at=issue["created_at"],
        started_at=started_at,
        started_signal=started_signal,
        done_at=done_at,
        outcome=outcome,
        labels=list(issue["labels"]),
        kinds=classify(issue["labels"], config),
        linked=sorted(links),
        reopen_count=sum(1 for e in events if e["event_type"] == "reopened"),
        excluded=reason is not None,
        exclude_reason=reason,
    )
    return item, _issue_events(item_id, issue, events, links)


def _issue_events(
    item_id: str, issue: Row, events: list[Row], links: dict[int, tuple[datetime, Row | None]]
) -> list[WorkItemEvent]:
    linking_ids = {event["id"] for _, event in links.values() if event is not None}
    out = [WorkItemEvent(item_id, issue["created_at"], "created", actor=issue.get("author"))]
    for event in events:
        kind = event["event_type"]
        if kind == "cross_referenced":
            if event["id"] not in linking_ids:
                continue  # a mention, or a link to a PR already linked earlier
            kind = "linked_pr"
        elif kind == "connected":
            if event["id"] not in linking_ids:
                continue
            kind = "linked_pr"
        detail = {
            "assigned": event.get("assignee"),
            "unassigned": event.get("assignee"),
            "labeled": event.get("label"),
            "unlabeled": event.get("label"),
            "linked_pr": f"#{event.get('pr_number')}",
            "closed": event.get("state_reason"),
        }.get(kind)
        out.append(
            WorkItemEvent(
                item_id,
                event["created_at"],
                kind,
                actor=event.get("actor"),
                detail=detail,
                source_id=event["id"],
            )
        )
    return out


def pull_request_work_item(
    pr: Row, reviews: list[Row], config: Config
) -> tuple[WorkItem, list[WorkItemEvent]]:
    """Turn a ``pull_requests`` row and its ``pr_reviews`` into a work item and its events.

    A PR's start isn't clamped to when it was opened: commits usually predate the PR, and
    that time is part of the work.
    """
    item_id = f"pr#{pr['number']}"
    if pr["state"] == "MERGED":
        done_at, outcome = pr["merged_at"], "merged"
    elif pr["state"] == "CLOSED":
        done_at, outcome = pr["closed_at"], "abandoned"
    else:
        done_at, outcome = None, "open"

    signals = {"first_commit": pr.get("first_commit_at"), "opened": pr["created_at"]}
    started_at, started_signal = derive_start(
        [(signal, signals[signal]) for signal in config.pr_started_signal], done_at=done_at
    )
    reason = exclusion_reason(
        state_reason=None, labels=pr["labels"], author_type=pr.get("author_type"), config=config
    )
    item = WorkItem(
        id=item_id,
        type="pr",
        number=pr["number"],
        title=pr.get("title"),
        url=pr.get("url"),
        author=pr.get("author"),
        author_type=pr.get("author_type"),
        created_at=pr["created_at"],
        started_at=started_at,
        started_signal=started_signal,
        done_at=done_at,
        outcome=outcome,
        labels=list(pr["labels"]),
        kinds=classify(pr["labels"], config),
        linked=sorted(pr.get("closing_issues") or []),
        reopen_count=0,
        excluded=reason is not None,
        exclude_reason=reason,
    )

    events = [WorkItemEvent(item_id, pr["created_at"], "created", actor=pr.get("author"))]
    if pr.get("first_commit_at"):
        events.append(WorkItemEvent(item_id, pr["first_commit_at"], "first_commit"))
    for review in sorted(
        (r for r in reviews if r.get("submitted_at")), key=lambda r: r["submitted_at"]
    ):
        events.append(
            WorkItemEvent(
                item_id,
                review["submitted_at"],
                f"review_{review['state'].lower()}",
                actor=review.get("author"),
                detail="bot" if review.get("author_type") == "Bot" else None,
                source_id=review["id"],
            )
        )
    if outcome == "merged":
        events.append(WorkItemEvent(item_id, done_at, "merged", detail=pr.get("merge_commit_sha")))
    elif outcome == "abandoned":
        events.append(WorkItemEvent(item_id, done_at, "closed"))
    return item, events
