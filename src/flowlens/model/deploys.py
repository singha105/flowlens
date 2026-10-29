"""Canonical deploys: one stream of production deployments from the configured source.

``deployment_source`` in flowlens.yaml picks where deploys come from:

- ``releases``: each published, non-draft release (pre-releases only if
  ``include_prereleases``), deployed when it was published.
- ``deployments``: GitHub Deployments to one of ``deployment_environments``. A deployment
  counts as deployed at its first SUCCESS status and as failed if it only ever reached
  FAILURE or ERROR. Successful deployments later go INACTIVE when a newer one replaces
  them, so the whole status history is read, not just the latest state.
- ``workflow:<name>``: completed runs of that Actions workflow, at completion time.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from flowlens.config import Config

Row = dict[str, Any]


@dataclass(frozen=True)
class Deploy:
    id: str
    source: str
    name: str | None
    sha: str | None
    environment: str | None
    status: str  # success | failure
    deployed_at: datetime
    url: str | None


def deploys_from_releases(releases: list[Row], *, include_prereleases: bool) -> list[Deploy]:
    return [
        Deploy(
            id=release["id"],
            source="releases",
            name=release["tag_name"],
            sha=release.get("target_sha"),
            environment=None,
            status="success",
            deployed_at=release["published_at"],
            url=release.get("url"),
        )
        for release in releases
        if release.get("published_at")
        and not release.get("is_draft")
        and (include_prereleases or not release.get("is_prerelease"))
    ]


def deploys_from_deployments(
    deployments: list[Row], statuses: list[Row], *, environments: tuple[str, ...]
) -> list[Deploy]:
    wanted = {env.lower() for env in environments}
    by_deployment: dict[str, list[Row]] = defaultdict(list)
    for status in statuses:
        by_deployment[status["deployment_id"]].append(status)

    deploys = []
    for deployment in deployments:
        if (deployment.get("environment") or "").lower() not in wanted:
            continue
        history = sorted(by_deployment.get(deployment["id"], []), key=lambda s: s["created_at"])
        success = next((s for s in history if s["state"] == "SUCCESS"), None)
        failure = next((s for s in history if s["state"] in {"FAILURE", "ERROR"}), None)
        if success is None and failure is None and deployment.get("latest_state") == "SUCCESS":
            # Statuses beyond the first page weren't fetched; trust the latest state.
            success = {"created_at": deployment["updated_at"]}
        if success is None and failure is None:
            continue  # still queued or in progress, or abandoned without an outcome
        outcome = success or failure
        deploys.append(
            Deploy(
                id=deployment["id"],
                source="deployments",
                name=deployment.get("ref_name"),
                sha=deployment.get("sha"),
                environment=deployment.get("environment"),
                status="success" if success else "failure",
                deployed_at=outcome["created_at"],
                url=None,
            )
        )
    return deploys


def deploys_from_workflow_runs(runs: list[Row], *, workflow: str) -> list[Deploy]:
    wanted = workflow.lower()
    deploys = []
    for run in runs:
        path = run.get("workflow_path") or ""
        names = {(run.get("workflow_name") or "").lower(), path.lower()}
        names.add(PurePosixPath(path).name.lower())
        if wanted not in names or run.get("status") != "completed":
            continue
        if run.get("conclusion") not in {"success", "failure"}:
            continue  # cancelled, skipped, timed out ... say nothing about the deploy
        deploys.append(
            Deploy(
                id=str(run["id"]),
                source=f"workflow:{workflow}",
                name=run.get("display_title") or run.get("workflow_name"),
                sha=run.get("head_sha"),
                environment=run.get("head_branch"),
                status=run["conclusion"],
                deployed_at=run["updated_at"],
                url=run.get("url"),
            )
        )
    return deploys


def resolve_deploys(
    config: Config,
    *,
    releases: list[Row],
    deployments: list[Row],
    statuses: list[Row],
    runs: list[Row],
) -> list[Deploy]:
    """Deploys from whichever source flowlens.yaml names."""
    if config.deployment_source == "releases":
        return deploys_from_releases(releases, include_prereleases=config.include_prereleases)
    if config.deployment_source == "deployments":
        return deploys_from_deployments(
            deployments, statuses, environments=config.deployment_environments
        )
    workflow = config.deployment_workflow
    assert workflow is not None, config.deployment_source
    return deploys_from_workflow_runs(runs, workflow=workflow)
