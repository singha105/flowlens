"""DORA's four keys from deploys, the PRs they shipped, and failure signals.

Every function here is pure and every result keeps the records behind it, so each number
can be traced to deploys, PRs, and issues. Definitions, explained in docs/METRICS.md:

- Deployment frequency: successful deploys in the window, per week and per day, and the
  median gap between consecutive ones.
- Lead time for changes: from a PR's first commit to the deploy that shipped it
  (flowlens.model.changes), for PRs shipped by deploys in the window. Coding time runs to
  the merge, waiting to deploy from the merge to the deploy.
- Change failure rate: the share of deploys in the window that a failure signal counts
  against. A merged revert counts against the deploy that shipped the PR it reverts; a
  merged PR or an issue labeled hotfix or incident counts against the last successful
  deploy before it was opened; a failed deployment status counts against itself. Each
  must fire within ``failure_window`` after the deploy.
- Failed deployment recovery time: from a failed deploy to the first fix for any of its
  signals: the deploy that ships the revert, the hotfix, or the incident's linked PR, or
  the incident issue closing when it has no linked PR. A failed status is fixed by the
  next successful deploy. A deploy that ships a fix can fail in turn; that's a failure of
  its own with its own recovery, since the signals can't tell whether it's the same one.

Medians and percentiles are nearest-rank, like the flow metrics.
"""

import bisect
import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from typing import Any

import numpy as np

from flowlens.metrics.flow import Window, percentiles
from flowlens.model.changes import SQUASH_REFERENCE, DeploymentChange, shipped_in_order
from flowlens.model.deploys import Deploy

FAILURE_KINDS = ("hotfix", "incident")
REVERT_TITLE = re.compile(r"^revert\b", re.IGNORECASE)
QUOTED_TITLE = re.compile(r'^revert\s+"(.+)"\s*$', re.IGNORECASE)  # GitHub's revert button
NUMBER = re.compile(r"(?:#|\bPR[- ]?)(\d+)\b", re.IGNORECASE)
REVERTS_COMMIT = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# Signal outcomes: counted against a deploy, or why not.
COUNTED = "counted"
OUTSIDE_WINDOW = "outside_window"  # fired too long after the deploy
NO_EARLIER_DEPLOY = "no_earlier_deploy"
NEVER_SHIPPED = "never_shipped"  # a revert of a PR no deploy shipped
BEFORE_SHIPPING = "before_shipping"  # a revert opened before its PR shipped


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str | None
    created_at: datetime
    merged_at: datetime | None
    first_commit_at: datetime | None
    merge_commit_sha: str | None
    base_ref_name: str | None
    kinds: tuple[str, ...] = ()
    excluded: bool = False


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    url: str | None
    created_at: datetime
    closed_at: datetime | None
    kinds: tuple[str, ...] = ()
    linked: tuple[int, ...] = ()  # PRs linked to it, including the one that closed it
    excluded: bool = False


@dataclass(frozen=True)
class History:
    """What the DORA metrics read: one repo's deploys and the work behind them."""

    deploys: list[Deploy]  # every deploy from the configured source, any status
    changes: dict[int, DeploymentChange]  # by PR number
    pull_requests: dict[int, PullRequest]
    issues: list[Issue] = field(default_factory=list)
    messages: dict[str, str] = field(default_factory=dict)  # commit SHA -> message

    def deploy(self, deploy_id: str) -> Deploy:
        return self._by_id[deploy_id]

    def shipped_by(self, number: int) -> Deploy | None:
        change = self.changes.get(number)
        return self._by_id[change.deploy_id] if change else None

    @cached_property
    def _by_id(self) -> dict[str, Deploy]:
        return {deploy.id: deploy for deploy in self.deploys}


@dataclass(frozen=True)
class Signal:
    """Evidence that a deploy failed, and whether it counts against one."""

    kind: str  # revert | hotfix | incident | failed_status
    ref: str  # "pr#123", "issue#45", or "deploy:<id>"
    title: str
    url: str | None
    at: datetime  # when it fired: the PR or issue was opened, or the deploy failed
    deploy_id: str | None  # the deploy it counts against, or the nearest one when it doesn't
    outcome: str  # COUNTED, or why it doesn't count
    reason: str
    fix_prs: tuple[int, ...] = ()  # PRs whose deploy ends the failure
    closed_at: datetime | None = None  # an incident issue's close, when it has no fix PR

    @property
    def counted(self) -> bool:
        return self.outcome == COUNTED


@dataclass(frozen=True)
class Recovery:
    deploy_id: str
    recovered_at: datetime | None  # None while no fix has shipped
    via: str | None = None  # the deploy that shipped the fix, or "issue #45 closed"
    reason: str = ""

    def hours(self, failed_at: datetime) -> float | None:
        if self.recovered_at is None:
            return None
        return (self.recovered_at - failed_at) / HOUR


# --- failure signals --------------------------------------------------------------------


def failure_signals(history: History, failure_window: timedelta) -> list[Signal]:
    """Every failure signal, with the deploy it counts against or why it doesn't count."""
    shipped = shipped_in_order(history.deploys)
    signals: list[Signal] = []
    reverts = set()
    for pr in history.pull_requests.values():
        if pr.merged_at is None or pr.excluded or not REVERT_TITLE.match(pr.title):
            continue
        reverts.add(pr.number)
        signals.append(_revert_signal(pr, history, shipped, failure_window))
    for pr in history.pull_requests.values():
        kind = _failure_kind(pr.kinds)
        if pr.merged_at is None or pr.excluded or kind is None or pr.number in reverts:
            continue
        deploy_id, outcome, when = _attribute(pr.created_at, shipped, failure_window)
        signals.append(
            Signal(
                kind=kind,
                ref=f"pr#{pr.number}",
                title=pr.title,
                url=pr.url,
                at=pr.created_at,
                deploy_id=deploy_id,
                outcome=outcome,
                reason=f"PR labeled {kind}, opened {when}",
                fix_prs=(pr.number,),
            )
        )
    for issue in history.issues:
        kind = _failure_kind(issue.kinds)
        if issue.excluded or kind is None:
            continue
        deploy_id, outcome, when = _attribute(issue.created_at, shipped, failure_window)
        fixes = _fix_prs(issue, deploy_id, history) if deploy_id else ()
        signals.append(
            Signal(
                kind=kind,
                ref=f"issue#{issue.number}",
                title=issue.title,
                url=issue.url,
                at=issue.created_at,
                deploy_id=deploy_id,
                outcome=outcome,
                reason=f"issue labeled {kind}, opened {when}",
                fix_prs=fixes,
                closed_at=None if fixes else issue.closed_at,
            )
        )
    for deploy in history.deploys:
        if deploy.status == "failure":
            signals.append(
                Signal(
                    kind="failed_status",
                    ref=f"deploy:{deploy.id}",
                    title=deploy.name or deploy.id,
                    url=deploy.url,
                    at=deploy.deployed_at,
                    deploy_id=deploy.id,
                    outcome=COUNTED,
                    reason="the deployment itself failed",
                )
            )
    return sorted(signals, key=lambda signal: (signal.at, signal.ref))


def reverted_pr(revert: PullRequest, history: History) -> int | None:
    """The merged PR a revert undoes: named in its title (``#123``, ``PR-123``), the PR
    whose title it quotes (``Revert "Title"``), or the commit its message says it reverts.
    """
    merged = history.pull_requests

    def usable(number: int) -> bool:
        pr = merged.get(number)
        return number != revert.number and pr is not None and pr.merged_at is not None

    for number in map(int, NUMBER.findall(revert.title)):
        if usable(number):
            return number
    quoted = QUOTED_TITLE.match(revert.title)
    if quoted:
        title = SQUASH_REFERENCE.sub("", quoted[1]).strip()
        same_title = [
            pr
            for pr in merged.values()
            if pr.title.strip() == title and usable(pr.number) and pr.merged_at <= revert.created_at
        ]
        if same_title:
            return max(same_title, key=lambda pr: pr.merged_at).number
    message = history.messages.get(revert.merge_commit_sha or "", "")
    for sha in REVERTS_COMMIT.findall(message):
        for pr in merged.values():
            if pr.merge_commit_sha and pr.merge_commit_sha.startswith(sha) and usable(pr.number):
                return pr.number
    return None


def _revert_signal(
    revert: PullRequest, history: History, shipped: list[Deploy], failure_window: timedelta
) -> Signal:
    common = {
        "kind": "revert",
        "ref": f"pr#{revert.number}",
        "title": revert.title,
        "url": revert.url,
        "at": revert.created_at,
        "fix_prs": (revert.number,),
    }
    target = reverted_pr(revert, history)
    if target is None:
        deploy_id, outcome, when = _attribute(revert.created_at, shipped, failure_window)
        reason = f"reverts a PR FlowLens can't identify; opened {when}"
        return Signal(**common, deploy_id=deploy_id, outcome=outcome, reason=reason)
    deploy = history.shipped_by(target)
    if deploy is None:
        reason = f"reverts #{target}, which no deploy shipped"
        return Signal(**common, deploy_id=None, outcome=NEVER_SHIPPED, reason=reason)
    after = revert.created_at - deploy.deployed_at
    if after <= timedelta(0):
        reason = f"reverts #{target}, opened before {_name(deploy)} shipped it"
        return Signal(**common, deploy_id=deploy.id, outcome=BEFORE_SHIPPING, reason=reason)
    reason = f"reverts #{target}, opened {_span(after)} after {_name(deploy)} shipped it"
    if after > failure_window:
        reason += ": outside the failure window"
        return Signal(**common, deploy_id=deploy.id, outcome=OUTSIDE_WINDOW, reason=reason)
    return Signal(**common, deploy_id=deploy.id, outcome=COUNTED, reason=reason)


def _attribute(
    at: datetime, shipped: list[Deploy], failure_window: timedelta
) -> tuple[str | None, str, str]:
    """The last successful deploy before ``at``, whether the signal counts against it,
    and when it fired relative to it."""
    index = bisect.bisect_left([deploy.deployed_at for deploy in shipped], at) - 1
    if index < 0:
        return None, NO_EARLIER_DEPLOY, "before the first deploy"
    deploy = shipped[index]
    after = at - deploy.deployed_at
    when = f"{_span(after)} after {_name(deploy)}"
    if after > failure_window:
        return deploy.id, OUTSIDE_WINDOW, f"{when}: outside the failure window"
    return deploy.id, COUNTED, when


def _fix_prs(issue: Issue, deploy_id: str, history: History) -> tuple[int, ...]:
    """Linked merged PRs that hadn't shipped by the failed deploy: the candidate fixes."""
    failed_at = history.deploy(deploy_id).deployed_at
    fixes = []
    for number in issue.linked:
        pr = history.pull_requests.get(number)
        if pr is None or pr.merged_at is None:
            continue
        shipped = history.shipped_by(number)
        if shipped is None or shipped.deployed_at > failed_at:
            fixes.append(number)
    return tuple(fixes)


def _failure_kind(kinds: tuple[str, ...]) -> str | None:
    return next((kind for kind in FAILURE_KINDS if kind in kinds), None)


# --- recovery ---------------------------------------------------------------------------


def counted_by_deploy(signals: list[Signal]) -> dict[str, list[Signal]]:
    by_deploy: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        if signal.counted:
            by_deploy[signal.deploy_id].append(signal)
    return dict(by_deploy)


def recoveries(history: History, signals: list[Signal]) -> dict[str, Recovery]:
    """How each failed deploy recovered (or that it hasn't yet), by deploy id."""
    shipped = shipped_in_order(history.deploys)
    result = {}
    for deploy_id, deploy_signals in counted_by_deploy(signals).items():
        deploy = history.deploy(deploy_id)
        options = [_recovery(signal, deploy, history, shipped) for signal in deploy_signals]
        done = [option for option in options if option.recovered_at is not None]
        if done:
            result[deploy_id] = min(done, key=lambda option: option.recovered_at)
        else:
            reasons = "; ".join(dict.fromkeys(option.reason for option in options))
            result[deploy_id] = Recovery(deploy_id, None, reason=reasons)
    return result


def _recovery(signal: Signal, deploy: Deploy, history: History, shipped: list[Deploy]):
    """When one signal's failure ended: its fix shipped, or its issue closed."""
    if signal.kind == "failed_status":
        fix = next((d for d in shipped if d.deployed_at > deploy.deployed_at), None)
        if fix is None:
            return Recovery(deploy.id, None, reason="no successful deploy since")
        return Recovery(deploy.id, fix.deployed_at, _name(fix), "the next successful deploy")
    fixes = sorted(
        (shipping.deployed_at, number, shipping)
        for number in signal.fix_prs
        if (shipping := history.shipped_by(number)) is not None
        and shipping.deployed_at > deploy.deployed_at
    )
    if fixes:
        deployed_at, number, fix = fixes[0]
        return Recovery(deploy.id, deployed_at, _name(fix), f"shipped #{number}")
    if signal.closed_at is not None and signal.closed_at > deploy.deployed_at:
        closed = signal.ref.replace("issue#", "issue #") + " closed"
        return Recovery(deploy.id, signal.closed_at, closed, "no linked PR to wait for")
    if signal.fix_prs:
        numbers = ", ".join(f"#{n}" for n in signal.fix_prs)
        return Recovery(deploy.id, None, reason=f"{numbers} not deployed yet")
    return Recovery(deploy.id, None, reason="no fix yet")


# --- the four keys ----------------------------------------------------------------------


def in_window(deploys: list[Deploy], window: Window) -> list[Deploy]:
    """Deploys in the window, any status, oldest first."""
    rows = [d for d in deploys if window.start <= d.deployed_at < window.end]
    return sorted(rows, key=lambda deploy: (deploy.deployed_at, deploy.id))


def deployment_frequency(deploys: list[Deploy], window: Window) -> dict[str, Any]:
    shipped = [d for d in in_window(deploys, window) if d.status == "success"]
    start = window.start.to_pydatetime()
    counts = Counter((d.deployed_at - start) // timedelta(weeks=1) for d in shipped)
    gaps = [
        (later.deployed_at - earlier.deployed_at) / DAY
        for earlier, later in itertools.pairwise(shipped)
    ]
    weekly = [
        {"week": week, "deploys": counts.get(i, 0)} for i, week in enumerate(window.week_starts())
    ]
    return {
        "deploys": len(shipped),
        "per_week": len(shipped) / window.weeks,
        "per_day": len(shipped) / (window.weeks * 7),
        "median_gap_days": percentiles(gaps)["p50"],
        "weeks_without": sum(row["deploys"] == 0 for row in weekly),
        "weekly": weekly,
    }


@dataclass(frozen=True)
class Change:
    """A merged PR and the deploy that shipped it."""

    pr: PullRequest
    deploy: Deploy
    matched_by: str

    @property
    def started_at(self) -> datetime:
        return self.pr.first_commit_at or self.pr.created_at

    @property
    def lead(self) -> timedelta:
        return self.deploy.deployed_at - self.started_at

    @property
    def coding(self) -> timedelta:
        return max(self.pr.merged_at - self.started_at, timedelta(0))

    @property
    def waiting(self) -> timedelta:
        return max(self.deploy.deployed_at - self.pr.merged_at, timedelta(0))


def shipped_changes(history: History, window: Window) -> list[Change]:
    """Counted PRs shipped by deploys in the window, in deploy order."""
    deploys = {d.id: d for d in in_window(history.deploys, window)}
    changes = []
    for number, change in history.changes.items():
        pr = history.pull_requests.get(number)
        if pr is None or pr.excluded or change.deploy_id not in deploys:
            continue
        changes.append(Change(pr, deploys[change.deploy_id], change.matched_by))
    return sorted(changes, key=lambda c: (c.deploy.deployed_at, c.pr.number))


def lead_time(changes: list[Change]) -> dict[str, Any]:
    lead = [c.lead / DAY for c in changes]
    coding = [c.coding / DAY for c in changes]
    waiting = [c.waiting / DAY for c in changes]
    total = sum(lead)
    return {
        "changes": len(changes),
        "p50_days": percentiles(lead)["p50"],
        "p85_days": percentiles(lead)["p85"],
        "coding_p50_days": percentiles(coding)["p50"],
        "waiting_p50_days": percentiles(waiting)["p50"],
        "waiting_share": sum(waiting) / total if total > 0 else None,
        "from_first_commit": sum(c.pr.first_commit_at is not None for c in changes),
    }


def not_shipped(history: History, window: Window) -> dict[str, Any]:
    """Counted PRs merged in the window that no deploy shipped, split by where they merged.

    The main line is the branch most shipped PRs merged into (most merged PRs, before
    anything has shipped); a PR merged there is waiting for a deploy. Elsewhere (a feature
    or release branch) it reaches production, if at all, through another PR.
    """
    prs = history.pull_requests
    bases = Counter(prs[n].base_ref_name for n in history.changes if n in prs)
    bases = bases or Counter(pr.base_ref_name for pr in prs.values() if pr.merged_at)
    main = bases.most_common(1)[0][0] if bases else None
    split: dict[str, Any] = {"main_line": main, "waiting": [], "other_branches": []}
    for pr in sorted(history.pull_requests.values(), key=lambda pr: pr.merged_at or pr.created_at):
        if pr.merged_at is None or pr.excluded or pr.number in history.changes:
            continue
        if window.start <= pr.merged_at < window.end:
            split["waiting" if pr.base_ref_name == main else "other_branches"].append(pr)
    return split


def change_failure_rate(
    deploys: list[Deploy], signals: list[Signal], window: Window
) -> dict[str, Any]:
    window_deploys = in_window(deploys, window)
    failed_ids = counted_by_deploy(signals)
    failed = [d for d in window_deploys if d.id in failed_ids]
    return {
        "deploys": len(window_deploys),
        "failed": len(failed),
        "rate": len(failed) / len(window_deploys) if window_deploys else None,
        "failed_deploys": failed,
    }


def recovery_time(failed: list[Deploy], recovered: dict[str, Recovery]) -> dict[str, Any]:
    hours = [recovered[d.id].hours(d.deployed_at) for d in failed]
    done = [h for h in hours if h is not None]
    return {
        "failed": len(failed),
        "recovered": len(done),
        "open": len(hours) - len(done),
        "median_hours": percentiles(done)["p50"],
        "max_hours": float(np.max(done)) if done else None,
    }


def _name(deploy: Deploy) -> str:
    return deploy.name or deploy.id


def _span(delta: timedelta) -> str:
    hours = delta / HOUR
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.1f}d"
