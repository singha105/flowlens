"""Synthetic work items with known arrival rates and duration distributions.

`generate(seed, scenario)` simulates a team day by day: issues and PRs arrive as Poisson
processes, wait before work starts, take lognormally distributed time to finish, and are
cut off at ``as_of`` the way an ingest would see them. That gives two kinds of ground truth
for the flow metrics:

- the scenario's distributions (`Scenario.cycle_quantile` and friends), which large samples
  must approach;
- brute-force oracles over the generated items (`wip_at`, `todo_at`, `done_between`, ...),
  written as plain loops so they share no code with the vectorised metrics they check.
"""

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

from flowlens.metrics.flow import pull_request_frame, work_item_frame
from flowlens.model.work_items import WorkItem

MONDAY = datetime(2026, 1, 5, tzinfo=UTC)
DELIVERED = {"completed", "closed_no_reason", "merged"}


@dataclass(frozen=True)
class Scenario:
    start: datetime = MONDAY
    days: int = 182
    issues_per_day: float = 3.0
    prs_per_day: float = 2.0
    # Issues: opened -> started is exponential, started -> closed is lognormal.
    start_wait_mean_days: float = 2.0
    cycle_median_days: float = 4.0
    cycle_sigma: float = 1.0
    # Issues closed without anyone starting them, this long after they were opened.
    never_started_share: float = 0.2
    never_started_median_days: float = 1.5
    not_planned_share: float = 0.05
    bug_share: float = 0.3
    # PR stages are lognormal with these medians (hours) and one shared shape.
    coding_hours: float = 6.0
    review_wait_hours: float = 20.0
    review_hours: float = 4.0
    merge_wait_hours: float = 2.0
    stage_sigma: float = 0.8
    unreviewed_share: float = 0.1
    abandoned_share: float = 0.05
    bot_share: float = 0.1

    @property
    def as_of(self) -> datetime:
        return self.start + timedelta(days=self.days)

    def cycle_quantile(self, p: float) -> float:
        """True p-th percentile (0-100) of issue cycle time, in days."""
        z = NormalDist().inv_cdf(p / 100)
        return self.cycle_median_days * math.exp(self.cycle_sigma * z)

    @property
    def cycle_mean_days(self) -> float:
        return self.cycle_median_days * math.exp(self.cycle_sigma**2 / 2)

    @property
    def issues_delivered_per_day(self) -> float:
        return self.issues_per_day * (1 - self.not_planned_share)

    @property
    def issues_started_per_day(self) -> float:
        return self.issues_delivered_per_day * (1 - self.never_started_share)

    @property
    def prs_merged_per_day(self) -> float:
        return self.prs_per_day * (1 - self.bot_share) * (1 - self.abandoned_share)


@dataclass
class Synthetic:
    scenario: Scenario
    items: list[WorkItem] = field(default_factory=list)
    pull_requests: list[dict] = field(default_factory=list)

    @property
    def as_of(self) -> datetime:
        return self.scenario.as_of

    def frame(self) -> pd.DataFrame:
        return work_item_frame(asdict(item) for item in self.items)

    def pr_frame(self) -> pd.DataFrame:
        return pull_request_frame(self.pull_requests)

    # --- brute-force oracles --------------------------------------------------------
    # State "at a cut" means just before that instant: started < cut, finished >= cut.

    def counted(self, type_: str | None = None) -> list[WorkItem]:
        return [i for i in self.items if not i.excluded and type_ in (None, i.type)]

    def wip_at(self, cut: datetime, type_: str | None = None) -> int:
        def in_progress(i: WorkItem) -> bool:
            if i.started_at is None or i.started_at >= cut:
                return False
            return i.done_at is None or i.done_at >= cut

        return sum(1 for i in self.counted(type_) if in_progress(i))

    def todo_at(self, cut: datetime, type_: str | None = None) -> int:
        def waiting(i: WorkItem) -> bool:
            started = i.started_at is not None and i.started_at < cut
            finished = i.done_at is not None and i.done_at < cut
            return i.created_at < cut and not started and not finished

        return sum(1 for i in self.counted(type_) if waiting(i))

    def done_between(
        self, start: datetime, end: datetime, type_: str | None = None
    ) -> list[WorkItem]:
        """Delivered items with start <= done_at < end."""
        return [
            i
            for i in self.counted(type_)
            if i.outcome in DELIVERED and i.done_at is not None and start <= i.done_at < end
        ]

    def lead_days(self, start: datetime, end: datetime, type_: str) -> list[float]:
        return [days(i.done_at - i.created_at) for i in self.done_between(start, end, type_)]

    def cycle_days(self, start: datetime, end: datetime, type_: str) -> list[float]:
        return [
            days(i.done_at - i.started_at)
            for i in self.done_between(start, end, type_)
            if i.started_at is not None
        ]

    def stage_hours(self, start: datetime, end: datetime) -> dict[str, list[float]]:
        """PR stage durations for counted PRs merged in [start, end)."""
        merged = {i.number for i in self.done_between(start, end, "pr")}
        stages: dict[str, list[float]] = {
            "coding": [],
            "waiting_for_review": [],
            "in_review": [],
            "waiting_to_merge": [],
        }
        for pr in self.pull_requests:
            if pr["number"] not in merged:
                continue
            path = [
                ("coding", pr["first_commit_at"], pr["created_at"]),
                ("waiting_for_review", pr["created_at"], pr["first_review_at"]),
                ("in_review", pr["first_review_at"], pr["first_approval_at"]),
                ("waiting_to_merge", pr["first_approval_at"], pr["merged_at"]),
            ]
            for stage, a, b in path:
                if a is not None and b is not None:
                    stages[stage].append(days(b - a) * 24)
        return stages


def nearest_rank(values: list[float], p: float) -> float | None:
    """The smallest value with at least p% of values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1]


def days(delta: timedelta) -> float:
    return delta.total_seconds() / 86400


def generate(seed: int, scenario: Scenario | None = None) -> Synthetic:
    s = scenario or Scenario()
    rng = np.random.default_rng(seed)
    out = Synthetic(s)
    numbers = iter(range(1, 10**7))

    def lognormal(median: float, sigma: float) -> float:
        return float(rng.lognormal(math.log(median), sigma))

    def seen(at: datetime | None) -> datetime | None:
        """What an ingest at as_of knows: nothing that hasn't happened yet."""
        return at if at is not None and at <= s.as_of else None

    for day in range(s.days):
        midnight = s.start + timedelta(days=day)

        for _ in range(rng.poisson(s.issues_per_day)):
            created = midnight + timedelta(days=float(rng.random()))
            kinds = ["bug"] if rng.random() < s.bug_share else []
            started = None
            if rng.random() < s.never_started_share:
                done = created + timedelta(days=lognormal(s.never_started_median_days, 0.5))
            else:
                started = created + timedelta(days=float(rng.exponential(s.start_wait_mean_days)))
                done = started + timedelta(days=lognormal(s.cycle_median_days, s.cycle_sigma))
            not_planned = rng.random() < s.not_planned_share
            started, done = seen(started), seen(done)
            outcome = "not_planned" if not_planned else "completed"
            if done is None:
                outcome = "open"
            out.items.append(
                _item("issue", next(numbers), created, started, done, outcome, kinds,
                      excluded=outcome == "not_planned")
            )  # fmt: skip

        for _ in range(rng.poisson(s.prs_per_day)):
            first_commit = midnight + timedelta(days=float(rng.random()))
            opened = first_commit + timedelta(hours=lognormal(s.coding_hours, s.stage_sigma))
            if opened > s.as_of:
                continue  # not on GitHub yet
            bot = rng.random() < s.bot_share
            review = approval = merged = closed = None
            if rng.random() < s.abandoned_share:
                closed = opened + timedelta(hours=lognormal(s.review_wait_hours, s.stage_sigma))
            elif rng.random() < s.unreviewed_share:
                merged = opened + timedelta(hours=lognormal(s.merge_wait_hours, s.stage_sigma))
            else:
                review = opened + timedelta(hours=lognormal(s.review_wait_hours, s.stage_sigma))
                approval = review + timedelta(hours=lognormal(s.review_hours, s.stage_sigma))
                merged = approval + timedelta(hours=lognormal(s.merge_wait_hours, s.stage_sigma))
            review, approval, merged, closed = map(seen, (review, approval, merged, closed))
            if merged is not None:
                outcome, done = "merged", merged
            elif closed is not None:
                outcome, done = "abandoned", closed
            else:
                outcome, done = "open", None
            number = next(numbers)
            out.items.append(
                _item("pr", number, opened, first_commit, done, outcome, [], excluded=bot,
                      author_type="Bot" if bot else "User")
            )  # fmt: skip
            out.pull_requests.append(
                {
                    "number": number,
                    "created_at": opened,
                    "first_commit_at": first_commit,
                    "first_review_at": review,
                    "first_approval_at": approval,
                    "merged_at": merged,
                }
            )
    return out


def work_item(
    number: int,
    *,
    created: datetime,
    started: datetime | None = None,
    done: datetime | None = None,
    outcome: str | None = None,
    type_: str = "issue",
    kinds: list[str] | None = None,
    excluded: bool = False,
) -> WorkItem:
    """A hand-built item for edge-case tests."""
    if outcome is None:
        outcome = "open" if done is None else ("merged" if type_ == "pr" else "completed")
    return _item(type_, number, created, started, done, outcome, kinds or [], excluded=excluded)


def _item(type_, number, created, started, done, outcome, kinds, *, excluded, author_type="User"):
    reason = None
    if excluded:
        reason = "bot" if author_type == "Bot" else "state_reason:NOT_PLANNED"
    return WorkItem(
        id=f"{type_}#{number}",
        type=type_,
        number=number,
        title=f"Synthetic {type_} {number}",
        url=f"https://github.com/acme/widgets/{'pull' if type_ == 'pr' else 'issues'}/{number}",
        author="sam",
        author_type=author_type,
        created_at=created,
        started_at=started,
        started_signal=None if started is None else "synthetic",
        done_at=done,
        outcome=outcome,
        labels=list(kinds),
        kinds=list(kinds),
        linked=[],
        reopen_count=0,
        excluded=excluded,
        exclude_reason=reason,
    )
