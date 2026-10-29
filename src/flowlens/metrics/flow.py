"""Agile flow metrics over the canonical work items.

Every function here is pure: it takes the ``work_items`` table (and, for PR flow, the
``pull_requests`` table) as DataFrames built by `work_item_frame` / `pull_request_frame`
and returns new values. Nothing reads the database or the clock, so each metric can be
checked against brute-force counts over synthetic data (tests/synthetic.py).

Conventions, explained in docs/METRICS.md:

- Delivered means done with outcome ``completed``, ``closed_no_reason`` or ``merged``.
  Abandoned PRs and excluded items (bots, not planned, duplicates) are never delivered.
- The window is whole ISO weeks, Monday 00:00 UTC, ending at the last Monday on or before
  ``as_of``. Lead time, cycle time, throughput and PR flow count items delivered inside it.
- A day's WIP and cumulative flow are the state at midnight UTC at the end of that day (or
  at ``as_of`` for the current day): started before the cut and not finished before it.
- Percentiles are nearest-rank: p85 is the smallest value with at least 85% of items at or
  below it, so "85% finished within p85" holds exactly.
"""

import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from flowlens.config import LABEL_KINDS

DONE_OUTCOMES = frozenset({"completed", "closed_no_reason", "merged"})
TYPES = ("issue", "pr")
PERCENTILES = (50, 85, 95)
ALL, OTHER = "all", "other"

# Histogram bins in days, each [low, high).
HISTOGRAM_EDGES = (0, 1, 2, 4, 7, 14, 30, 60, 90, 180, math.inf)
HISTOGRAM_BINS = tuple(
    f"{low}-{high}d" if high != math.inf else f"{low}d+"
    for low, high in itertools.pairwise(HISTOGRAM_EDGES)
)

# PR flow: (stage, from, to) over pull_requests columns.
PR_STAGES = (
    ("coding", "first_commit_at", "created_at"),
    ("waiting_for_review", "created_at", "first_review_at"),
    ("in_review", "first_review_at", "first_approval_at"),
    ("waiting_to_merge", "first_approval_at", "merged_at"),
)

WORK_ITEM_TIMES = ("created_at", "started_at", "done_at")
PULL_REQUEST_TIMES = ("created_at", "first_commit_at", "first_review_at", "first_approval_at",
                      "merged_at")  # fmt: skip
DAY = pd.Timedelta(days=1)
WEEK = pd.Timedelta(weeks=1)


@dataclass(frozen=True)
class Window:
    """``weeks`` whole ISO weeks, [start, end), ending at the last Monday on or before as_of."""

    start: pd.Timestamp
    end: pd.Timestamp
    as_of: pd.Timestamp

    @classmethod
    def ending(cls, as_of: datetime | pd.Timestamp, weeks: int) -> "Window":
        if weeks < 1:
            raise ValueError("weeks must be at least 1")
        as_of = pd.Timestamp(as_of)
        if as_of.tzinfo is None:
            raise ValueError(f"naive as_of {as_of!r}; FlowLens only works in UTC")
        as_of = as_of.tz_convert("UTC")
        midnight = as_of.normalize()
        end = midnight - pd.Timedelta(days=midnight.weekday())
        return cls(start=end - weeks * WEEK, end=end, as_of=as_of)

    @property
    def weeks(self) -> int:
        return (self.end - self.start) // WEEK

    def week_starts(self) -> pd.DatetimeIndex:
        return pd.date_range(self.start, self.end, freq="7D", inclusive="left")

    def days(self) -> pd.DatetimeIndex:
        """Every day from the window start up to the one ``as_of`` falls in."""
        return pd.date_range(self.start, self.as_of, freq="D", inclusive="left")


# --- frames -----------------------------------------------------------------------------


def work_item_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """``work_items`` rows (dicts, as Store.rows returns them) as a typed DataFrame."""
    columns = ("id", "type", "number", "title", "url", *WORK_ITEM_TIMES, "outcome", "kinds",
               "excluded")  # fmt: skip
    frame = _frame(rows, columns, WORK_ITEM_TIMES)
    frame["kinds"] = frame["kinds"].map(lambda kinds: list(kinds) if kinds is not None else [])
    frame["excluded"] = frame["excluded"].astype(bool)
    frame["number"] = frame["number"].astype("int64")
    return frame


def pull_request_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """``pull_requests`` rows as a typed DataFrame (only the columns PR flow needs)."""
    frame = _frame(rows, ("number", *PULL_REQUEST_TIMES), PULL_REQUEST_TIMES)
    frame["number"] = frame["number"].astype("int64")
    return frame


def _frame(rows: Iterable[dict], columns: tuple[str, ...], times: tuple[str, ...]):
    frame = pd.DataFrame([{col: row.get(col) for col in columns} for row in rows], columns=columns)
    for column in times:
        frame[column] = pd.to_datetime(frame[column], utc=True).astype("datetime64[us, UTC]")
    return frame


def counted(items: pd.DataFrame) -> pd.DataFrame:
    """Items the metrics count: everything not excluded."""
    return items[~items["excluded"]]


def delivered(items: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Counted items delivered inside the window."""
    rows = counted(items)
    done_at = rows["done_at"]
    inside = (done_at >= window.start) & (done_at < window.end)
    return rows[rows["outcome"].isin(DONE_OUTCOMES) & inside]


# --- lead and cycle time ----------------------------------------------------------------


def durations(items: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Lead and cycle time in days for every item delivered in the window.

    Lead time runs from created_at, cycle time from started_at, both to done_at. Cycle time
    is NaN for an item that never started. A PR's first commit can be dated after its merge
    (rewritten author dates); such cycle times count as 0.
    """
    rows = delivered(items, window)
    return pd.DataFrame(
        {
            "id": rows["id"],
            "type": rows["type"],
            "number": rows["number"],
            "kinds": rows["kinds"],
            "lead_days": _days(rows["done_at"] - rows["created_at"]).clip(lower=0),
            "cycle_days": _days(rows["done_at"] - rows["started_at"]).clip(lower=0),
        }
    )


def percentiles(values: Iterable[float]) -> dict[str, float | None]:
    """Nearest-rank p50, p85 and p95, ignoring NaN; None when there are no values."""
    array = _values(values)
    if not len(array):
        return {f"p{p}": None for p in PERCENTILES}
    return {f"p{p}": float(np.percentile(array, p, method="inverted_cdf")) for p in PERCENTILES}


def histogram(values: Iterable[float]) -> dict[str, int]:
    """Counts per HISTOGRAM_BINS, ignoring NaN."""
    array = _values(values)
    index = np.searchsorted(HISTOGRAM_EDGES, array, side="right") - 1
    counts = np.bincount(index, minlength=len(HISTOGRAM_BINS))
    return dict(zip(HISTOGRAM_BINS, (int(n) for n in counts), strict=True))


def summarize(values: Iterable[float]) -> dict[str, Any]:
    array = _values(values)
    return {"count": len(array), **percentiles(array), "histogram": histogram(array)}


def time_summary(durations: pd.DataFrame, column: str) -> dict[str, dict[str, dict]]:
    """``{type: {group: summary}}`` for ``lead_days`` or ``cycle_days``.

    Groups are "all", one per label kind (an item labelled bug and incident is in both), and
    "other" for items with no kind.
    """
    return {
        type_: {
            group: summarize(rows[column])
            for group, rows in label_groups(durations[durations["type"] == type_]).items()
        }
        for type_ in TYPES
    }


def label_groups(rows: pd.DataFrame) -> dict[str, pd.DataFrame]:
    def having(test) -> pd.DataFrame:
        # astype(bool): mapping an empty Series gives object dtype, which would select columns.
        return rows.loc[rows["kinds"].map(test).astype(bool)]

    groups = {ALL: rows}
    for kind in LABEL_KINDS:
        groups[kind] = having(lambda kinds, kind=kind: kind in kinds)
    groups[OTHER] = having(lambda kinds: not kinds)
    return groups


# --- throughput -------------------------------------------------------------------------


def weekly_throughput(items: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Items delivered per ISO week, by type and in total; weeks with none are 0.

    The index is each week's Monday. The week ``as_of`` falls in isn't over yet, so it's
    left out.
    """
    rows = delivered(items, window)
    week = rows["done_at"].dt.normalize() - pd.to_timedelta(rows["done_at"].dt.day_of_week, "D")
    index = window.week_starts()
    table = pd.DataFrame(
        {
            type_: week[rows["type"] == type_].value_counts().reindex(index, fill_value=0)
            for type_ in TYPES
        },
        index=index,
    )
    table["total"] = table.sum(axis=1)
    table.index.name = "week"
    return table.astype(int)


def throughput_summary(weekly: pd.Series) -> dict[str, Any]:
    return {
        "mean": float(weekly.mean()) if len(weekly) else None,
        "min": int(weekly.min()) if len(weekly) else None,
        "max": int(weekly.max()) if len(weekly) else None,
        "zero_weeks": int((weekly == 0).sum()),
    }


# --- WIP and cumulative flow ------------------------------------------------------------


def cumulative_flow(items: pd.DataFrame, window: Window, type_: str | None = None):
    """Daily to do, in progress, and done for one type (or all items).

    At each day's cut (midnight UTC at its end, or as_of):

    - to do: opened before the cut, not yet started or finished;
    - in progress: started before the cut, not finished before it (WIP);
    - done: delivered since the window started.

    An item that finishes without being delivered (an abandoned PR) just leaves the chart.
    """
    rows = counted(items)
    if type_ is not None:
        rows = rows[rows["type"] == type_]
    days = window.days()
    cuts = _instants((days + DAY).where(days + DAY <= window.as_of, window.as_of))

    finish = rows["done_at"]
    # A start dated after the finish (rewritten PR commit dates) counts as the finish.
    started = rows["started_at"].where(finish.isna() | (rows["started_at"] <= finish), finish)
    left_todo = started.fillna(finish)
    created = rows["created_at"]
    delivered_at = finish[rows["outcome"].isin(DONE_OUTCOMES)]

    # An item can leave to do before it was opened (a PR's first commit): it never waited.
    leaves_todo_after_opening = created.where(created >= left_todo, left_todo)
    todo = _before(created, cuts) - _before(leaves_todo_after_opening, cuts)
    in_progress = _before(started, cuts) - _before(finish[started.notna()], cuts)
    done = _before(delivered_at, cuts) - _before(delivered_at, _instants([window.start]))
    frame = pd.DataFrame({"todo": todo, "in_progress": in_progress, "done": done}, index=days)
    frame.index.name = "day"
    return frame


def daily_wip(items: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Items in progress at the end of each day, by type and in total."""
    table = pd.DataFrame(
        {type_: cumulative_flow(items, window, type_)["in_progress"] for type_ in TYPES}
    )
    table["total"] = cumulative_flow(items, window)["in_progress"]
    return table


# --- aging WIP --------------------------------------------------------------------------


def aging_wip(
    items: pd.DataFrame, as_of: pd.Timestamp, cycle: dict[str, dict[str, float | None]]
) -> pd.DataFrame:
    """Every item in progress at as_of, oldest first, against its type's cycle time.

    ``cycle`` maps a type to its cycle-time percentiles (``{"p50": ..., "p85": ...}``).
    ``over_p85`` is True when the item is already older than 85% of finished items took.
    """
    rows = counted(items)
    started, finish = rows["started_at"], rows["done_at"]
    active = rows[(started < as_of) & (finish.isna() | (finish >= as_of))]
    p50 = active["type"].map(lambda t: cycle.get(t, {}).get("p50")).astype(float)
    p85 = active["type"].map(lambda t: cycle.get(t, {}).get("p85")).astype(float)
    age = _days(as_of - active["started_at"])
    frame = pd.DataFrame(
        {
            "id": active["id"],
            "type": active["type"],
            "number": active["number"],
            "title": active["title"],
            "url": active["url"],
            "started_at": active["started_at"],
            "age_days": age,
            "cycle_p50": p50,
            "cycle_p85": p85,
            "over_p50": age > p50,
            "over_p85": age > p85,
        }
    )
    return frame.sort_values(["age_days", "id"], ascending=[False, True], ignore_index=True)


# --- PR flow ----------------------------------------------------------------------------


def pr_stages(items: pd.DataFrame, prs: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Hours in each PR_STAGES stage for every counted PR merged inside the window.

    A stage is NaN when either end is missing: no human review, no approval. A review or
    approval after the merge didn't hold the PR up, so it counts as missing too. A stage
    that would be negative (a first commit dated after the PR opened) counts as 0.
    """
    merged = delivered(items, window)
    merged = merged.loc[merged["type"] == "pr", ["number"]]
    rows = prs.merge(merged, on="number", how="inner")
    points = {column: rows[column] for column in PULL_REQUEST_TIMES}
    for column in ("first_review_at", "first_approval_at"):
        points[column] = points[column].where(points[column] <= rows["merged_at"])
    frame = pd.DataFrame({"number": rows["number"], "merged_at": rows["merged_at"]})
    for stage, start, end in PR_STAGES:
        frame[stage] = (_days(points[end] - points[start]) * 24).clip(lower=0)
    return frame


def pr_flow(stages: pd.DataFrame) -> dict[str, Any]:
    """Median hours per stage and the bottleneck: the stage with the longest median.

    ``bottleneck_share`` is that median over the sum of all four stage medians.
    """
    summary = {}
    for stage, *_ in PR_STAGES:
        values = stages[stage].dropna()
        summary[stage] = {
            "count": len(values),
            "median_hours": float(values.median()) if len(values) else None,
        }
    medians = {
        stage: s["median_hours"] for stage, s in summary.items() if s["median_hours"] is not None
    }
    total = sum(medians.values())
    bottleneck = max(medians, key=medians.__getitem__) if total > 0 else None
    return {
        "merged": len(stages),
        "stages": summary,
        "bottleneck": bottleneck,
        "bottleneck_share": medians[bottleneck] / total if bottleneck else None,
    }


def pr_flow_by_month(stages: pd.DataFrame) -> pd.DataFrame:
    """Per calendar month of merge: PRs merged, share with a human review, stage medians."""
    month = stages["merged_at"].dt.strftime("%Y-%m")
    grouped = stages.groupby(month)
    frame = pd.DataFrame(
        {
            "merged": grouped.size(),
            "reviewed_share": grouped["waiting_for_review"].agg(lambda s: s.notna().mean()),
            **{stage: grouped[stage].median() for stage, *_ in PR_STAGES},
        }
    )
    frame.index.name = "month"
    return frame


# --- helpers ----------------------------------------------------------------------------


def _days(delta: pd.Series) -> pd.Series:
    return delta.dt.total_seconds() / 86400


def _values(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[~np.isnan(array)]


def _instants(times) -> np.ndarray:
    """Sorted UTC instants as datetime64[us], NaT dropped."""
    index = pd.DatetimeIndex(times).dropna()
    return np.sort(index.tz_convert("UTC").tz_localize(None).to_numpy("datetime64[us]"))


def _before(times, cuts: np.ndarray) -> np.ndarray:
    """How many of ``times`` fall strictly before each cut."""
    return np.searchsorted(_instants(times), cuts, side="left")
