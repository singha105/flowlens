"""Rebuild the model tables from the source tables in one pass."""

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from flowlens.config import Config
from flowlens.model.deploys import resolve_deploys
from flowlens.model.work_items import issue_work_item, pull_request_work_item
from flowlens.storage import Store


@dataclass(frozen=True)
class ModelCounts:
    work_items: int
    events: int
    deploys: int


def build_model(store: Store, config: Config, repo: str) -> ModelCounts:
    """Derive work_items, work_item_events, and deploys, replacing what was there.

    The model is cheap to rebuild and depends on flowlens.yaml, so it's never patched in
    place: change the config, rebuild, and every item reflects the new rules.
    """
    pull_requests = store.rows("pull_requests")
    events_by_issue = _group(store.rows("issue_events"), "issue_number")
    reviews_by_pr = _group(store.rows("pr_reviews"), "pr_number")
    closing_prs: dict[int, dict[int, datetime]] = defaultdict(dict)
    for pr in pull_requests:
        for issue_number in pr.get("closing_issues") or []:
            closing_prs[issue_number][pr["number"]] = pr["created_at"]

    items, events = [], []
    for issue in store.rows("issues"):
        item, item_events = issue_work_item(
            issue,
            events_by_issue.get(issue["number"], []),
            config,
            repo=repo,
            closing_prs=closing_prs.get(issue["number"]),
        )
        items.append(item)
        events.extend(item_events)
    for pr in pull_requests:
        item, item_events = pull_request_work_item(pr, reviews_by_pr.get(pr["number"], []), config)
        items.append(item)
        events.extend(item_events)

    deploys = resolve_deploys(
        config,
        releases=store.rows("releases"),
        deployments=store.rows("deployments"),
        statuses=store.rows("deployment_statuses"),
        runs=store.rows("workflow_runs"),
    )
    store.replace(
        {
            "work_items": [asdict(item) for item in items],
            "work_item_events": [asdict(event) for event in events],
            "deploys": [asdict(deploy) for deploy in deploys],
        }
    )
    return ModelCounts(len(items), len(events), len(deploys))


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return grouped
