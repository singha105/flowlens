"""Which deploy shipped each merged PR.

Each successful deploy ships the commits since the previous successful deploy: the
compare range ``previous.sha...deploy.sha``, fetched at ingest time. A merged PR shipped
in the first deploy whose range holds either its merge commit or a commit that names it
the way squash merges and cherry-picks do (``Title (#123)``, ``Merge pull request #123``).
A cherry-pick into a patch release therefore ships the PR before the release that later
carries its original merge commit.

The first deploy has nothing to compare with, so what it shipped is unknown. A deploy
without a commit SHA gets no range either, and the next deploy's range starts from the
last deploy that had one.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from flowlens.ingest.delivery import compare_id
from flowlens.model.deploys import Deploy

Row = dict[str, Any]

SQUASH_REFERENCE = re.compile(r"\(#(\d+)\)\s*$")  # "Fix the thing (#123)"
MERGE_REFERENCE = re.compile(r"^Merge pull request #(\d+)\b")


@dataclass(frozen=True)
class Range:
    """The commits one deploy shipped: those in ``head_sha`` but not in ``base_sha``."""

    deploy_id: str
    base_sha: str | None  # None for the first deploy
    head_sha: str | None  # None when the deploy has no commit

    @property
    def compare_id(self) -> str | None:
        if self.base_sha is None or self.head_sha is None or self.base_sha == self.head_sha:
            return None
        return compare_id(self.base_sha, self.head_sha)


@dataclass(frozen=True)
class DeploymentChange:
    pr_number: int
    deploy_id: str
    commit_sha: str
    matched_by: str  # merge_commit | pr_reference


def shipped_in_order(deploys: list[Deploy]) -> list[Deploy]:
    """Successful deploys, oldest first: the ones that put code in production."""
    return sorted(
        (deploy for deploy in deploys if deploy.status == "success"),
        key=lambda deploy: (deploy.deployed_at, deploy.id),
    )


def deploy_ranges(deploys: list[Deploy]) -> list[Range]:
    """One range per successful deploy, oldest first."""
    ranges, base = [], None
    for deploy in shipped_in_order(deploys):
        ranges.append(Range(deploy.id, base, deploy.sha))
        if deploy.sha:
            base = deploy.sha
    return ranges


def referenced_pr(message: str) -> int | None:
    """The PR a commit's subject line names, if it follows a merge or squash convention."""
    subject = message.split("\n", 1)[0]
    match = SQUASH_REFERENCE.search(subject) or MERGE_REFERENCE.match(subject)
    return int(match[1]) if match else None


def map_changes(
    deploys: list[Deploy], commits: list[Row], pull_requests: list[Row]
) -> list[DeploymentChange]:
    """The deploy that first shipped each merged PR, from the stored compare commits."""
    merged = {pr["number"] for pr in pull_requests if pr.get("merged_at")}
    by_merge_commit = {
        pr["merge_commit_sha"]: pr["number"]
        for pr in pull_requests
        if pr.get("merged_at") and pr.get("merge_commit_sha")
    }
    by_range: dict[str, list[Row]] = defaultdict(list)
    for commit in sorted(commits, key=lambda commit: commit["position"]):
        by_range[commit["compare_id"]].append(commit)

    changes: dict[int, DeploymentChange] = {}
    for deploy_range in deploy_ranges(deploys):
        for commit in by_range.get(deploy_range.compare_id or "", []):
            number, matched_by = by_merge_commit.get(commit["sha"]), "merge_commit"
            if number is None:
                number, matched_by = referenced_pr(commit["message"]), "pr_reference"
            if number in merged and number not in changes:
                changes[number] = DeploymentChange(
                    number, deploy_range.deploy_id, commit["sha"], matched_by
                )
    return list(changes.values())
