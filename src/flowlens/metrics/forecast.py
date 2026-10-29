"""Monte Carlo delivery forecasts from weekly throughput, and their backtest.

A forecast resamples the last ``history_weeks`` complete weeks of throughput (the series
from flowlens.metrics.flow.weekly_throughput), with replacement and zero weeks included,
``trials`` times:

- `when`: how many weeks until ``n_items`` are done, as dates;
- `how_many`: how many items get done in the whole weeks from ``start`` to ``by``.

Both read only weeks that ended on or before ``start``, so a forecast can't see what came
after it. `backtest` replays the forecast from past weeks and scores it against what
actually happened.

Confidence means the same thing in both directions. At 85%, `when` gives the date by which
85% of trials had finished, and `how_many` gives the count that 85% of trials reached or
beat. So the cautious answer is the high percentile for a date and the low one for a count.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

CONFIDENCES = (50, 85, 95)
WEEK = pd.Timedelta(weeks=1)
DAY = pd.Timedelta(days=1)
MAX_WEEKS = 520  # ten years; a forecast further out than that means nothing

BACKTEST_HORIZONS = (4, 8)
BACKTEST_HISTORIES = (6, 12, 26)
BACKTEST_ORIGINS = 52


class ForecastError(ValueError):
    """The history can't support the forecast asked for."""


def history_before(weekly: pd.Series, start: date | datetime, weeks: int) -> np.ndarray:
    """The last ``weeks`` complete weeks of throughput that ended by ``start``.

    ``weekly`` is indexed by each week's Monday (UTC), one row per week, zeros included.
    """
    if weeks < 1:
        raise ValueError("history_weeks must be at least 1")
    start = to_utc(start)
    ended = weekly[weekly.index + WEEK <= start]
    if len(ended) < weeks:
        raise ForecastError(
            f"{weeks} weeks of history needed before {start:%Y-%m-%d}, but only "
            f"{len(ended)} complete weeks are available"
        )
    return ended.iloc[-weeks:].to_numpy(dtype=float)


# --- when -------------------------------------------------------------------------------


@dataclass(frozen=True)
class WhenForecast:
    n_items: int
    start: pd.Timestamp
    history: np.ndarray
    weeks: np.ndarray  # whole weeks each trial took to finish n_items

    @property
    def trials(self) -> int:
        return len(self.weeks)

    def weeks_at(self, confidence: int) -> int:
        """The fewest weeks that confidence% of trials finished within."""
        return int(np.percentile(self.weeks, confidence, method="inverted_cdf"))

    def date_at(self, confidence: int) -> pd.Timestamp:
        """Done before this date (the Monday after the finishing week), at confidence%."""
        return self.start + self.weeks_at(confidence) * WEEK

    @property
    def dates(self) -> dict[int, pd.Timestamp]:
        return {c: self.date_at(c) for c in CONFIDENCES}

    def distribution(self) -> pd.DataFrame:
        """Share of trials finishing in each week, and cumulatively."""
        counts = pd.Series(self.weeks).value_counts().sort_index()
        frame = pd.DataFrame(
            {
                "weeks": counts.index,
                "date": [self.start + int(w) * WEEK for w in counts.index],
                "share": counts.to_numpy() / self.trials,
            }
        )
        frame["cumulative"] = frame["share"].cumsum()
        return frame


def when(
    weekly: pd.Series,
    n_items: int,
    start: date | datetime,
    *,
    history_weeks: int = 12,
    trials: int = 10_000,
    seed: int | np.random.Generator | None = None,
    throughput_factor: float = 1.0,
) -> WhenForecast:
    """When will ``n_items`` more items be done, starting at ``start``?

    Each trial draws weeks from the history until the running total reaches ``n_items``.
    ``throughput_factor`` scales every historical week (0.8 = a 20% drop).
    """
    if n_items < 0:
        raise ValueError("n_items can't be negative")
    start = to_utc(start)
    history = history_before(weekly, start, history_weeks) * throughput_factor
    if n_items and not history.any():
        raise ForecastError(
            f"nothing was delivered in the {history_weeks} weeks before {start:%Y-%m-%d}, "
            "so there is no throughput to resample"
        )
    rng = np.random.default_rng(seed)
    weeks = _weeks_to_finish(rng, history, n_items, trials)
    return WhenForecast(n_items=n_items, start=start, history=history, weeks=weeks)


def _weeks_to_finish(
    rng: np.random.Generator, history: np.ndarray, n_items: int, trials: int
) -> np.ndarray:
    """Weeks until each trial's running total reaches n_items, drawn a block at a time."""
    weeks = np.zeros(trials, dtype=np.int64)
    if n_items == 0:
        return weeks
    done = np.zeros(trials)
    active = np.arange(trials)
    # Enough weeks per block that most trials finish in the first one.
    block = int(min(MAX_WEEKS, max(8, 2 * math.ceil(n_items / history.mean()))))
    elapsed = 0
    while active.size:
        if elapsed >= MAX_WEEKS:
            raise ForecastError(
                f"at this throughput some trials take more than {MAX_WEEKS} weeks "
                f"to finish {n_items} items"
            )
        totals = done[active, None] + rng.choice(history, size=(active.size, block)).cumsum(1)
        reached = totals >= n_items - 1e-9  # scaled history isn't whole numbers
        finished = reached.any(axis=1)
        weeks[active[finished]] = elapsed + reached[finished].argmax(axis=1) + 1
        done[active] = totals[:, -1]
        active = active[~finished]
        elapsed += block
    return weeks


# --- how many ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HowManyForecast:
    start: pd.Timestamp
    weeks: int  # whole weeks from start through by
    history: np.ndarray
    items: np.ndarray  # items each trial finished

    @property
    def trials(self) -> int:
        return len(self.items)

    @property
    def through(self) -> pd.Timestamp:
        """The last day counted: the Sunday ending the last whole week."""
        return self.start + self.weeks * WEEK - DAY

    def at_least(self, confidence: int) -> int:
        """The most items that confidence% of trials reached or beat."""
        return int(-np.percentile(-self.items, confidence, method="inverted_cdf"))

    @property
    def counts(self) -> dict[int, int]:
        return {c: self.at_least(c) for c in CONFIDENCES}

    def distribution(self) -> pd.DataFrame:
        """Share of trials finishing each count, and the share reaching at least it."""
        counts = pd.Series(self.items).value_counts().sort_index()
        frame = pd.DataFrame({"items": counts.index, "share": counts.to_numpy() / self.trials})
        frame["at_least"] = frame["share"][::-1].cumsum()[::-1]
        return frame


def how_many(
    weekly: pd.Series,
    by: date | datetime,
    start: date | datetime,
    *,
    history_weeks: int = 12,
    trials: int = 10_000,
    seed: int | np.random.Generator | None = None,
    throughput_factor: float = 1.0,
) -> HowManyForecast:
    """How many items will be done from ``start`` through the day ``by``?

    Only whole weeks count: from a Monday start, ``by`` on a Thursday leaves out the last
    four days, which keeps the forecast on the cautious side.
    """
    start, by = to_utc(start), to_utc(by)
    if by < start:
        raise ValueError(f"by ({by:%Y-%m-%d}) is before start ({start:%Y-%m-%d})")
    weeks = (by + DAY - start) // WEEK
    history = history_before(weekly, start, history_weeks) * throughput_factor
    rng = np.random.default_rng(seed)
    totals = rng.choice(history, size=(trials, weeks)).sum(axis=1)
    items = np.floor(totals + 1e-9).astype(np.int64)
    return HowManyForecast(start=start, weeks=weeks, history=history, items=items)


# --- backtest ---------------------------------------------------------------------------


def backtest_origins(
    weekly: pd.Series, horizon: int, history_weeks: int, origins: int
) -> pd.DatetimeIndex:
    """The latest ``origins`` weeks with ``history_weeks`` before and ``horizon`` after."""
    first, last = history_weeks, len(weekly) - horizon  # positions in the series
    return weekly.index[max(first, last - origins + 1) : last + 1]


def backtest(
    weekly: pd.Series,
    *,
    horizons: tuple[int, ...] = BACKTEST_HORIZONS,
    histories: tuple[int, ...] = BACKTEST_HISTORIES,
    origins: int = BACKTEST_ORIGINS,
    trials: int = 10_000,
    seed: int = 0,
) -> pd.DataFrame:
    """Forecast "how many in the next ``horizon`` weeks" from past weeks; score each one.

    For each horizon, the origins are the latest ``origins`` weeks that have the longest
    history before them and a whole horizon after them, so every history length is scored
    on the same weeks. Each forecast sees only the weeks before its origin (`how_many`
    enforces it); the actual count is what the series shows for the horizon.

    One row per (horizon, history, origin): the 50/85/95% counts, the actual count, and
    whether each held (actual >= the forecast count).
    """
    rows = []
    for horizon in horizons:
        for origin in backtest_origins(weekly, horizon, max(histories), origins):
            ahead = weekly[(weekly.index >= origin) & (weekly.index < origin + horizon * WEEK)]
            actual = int(ahead.sum())
            for history in histories:
                rng = np.random.default_rng([seed, horizon, history, origin.value // 10**9])
                forecast = how_many(
                    weekly,
                    origin + horizon * WEEK - DAY,
                    origin,
                    history_weeks=history,
                    trials=trials,
                    seed=rng,
                )
                counts = forecast.counts
                rows.append(
                    {
                        "horizon_weeks": horizon,
                        "history_weeks": history,
                        "origin": origin,
                        **{f"p{c}": n for c, n in counts.items()},
                        "actual": actual,
                        **{f"held_{c}": actual >= n for c, n in counts.items()},
                    }
                )
    columns = ["horizon_weeks", "history_weeks", "origin", "p50", "p85", "p95", "actual",
               "held_50", "held_85", "held_95"]  # fmt: skip
    return pd.DataFrame(rows, columns=columns)


def score_backtest(rows: pd.DataFrame) -> pd.DataFrame:
    """Per (horizon, history): how often each confidence held, and the p50's error.

    ``mae`` is the mean absolute error of the p50 count; ``bias`` is the mean of actual
    minus p50, so a positive bias means the forecasts ran low.
    """
    error = rows["actual"] - rows["p50"]
    scored = rows.assign(error=error, abs_error=error.abs())
    grouped = scored.groupby(["horizon_weeks", "history_weeks"])
    return pd.DataFrame(
        {
            "origins": grouped.size(),
            "first_origin": grouped["origin"].min(),
            "last_origin": grouped["origin"].max(),
            **{f"held_{c}": grouped[f"held_{c}"].mean() for c in CONFIDENCES},
            "mae": grouped["abs_error"].mean(),
            "bias": grouped["error"].mean(),
            "mean_actual": grouped["actual"].mean(),
        }
    )


def to_utc(value: date | datetime | pd.Timestamp) -> pd.Timestamp:
    """A Timestamp in UTC; dates and naive datetimes are taken as UTC."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
