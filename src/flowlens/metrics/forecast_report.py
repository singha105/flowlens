"""``flowlens forecast`` and ``flowlens backtest`` for one repo: load, run, and print.

The throughput series comes from flowlens.metrics.flow over every week the ingested
history fully covers: from the first Monday on or after ``--since`` (an item finished then
was updated then, so it's in the data) to the last Monday on or before the last ingest.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from flowlens.metrics import flow
from flowlens.metrics.forecast import (
    BACKTEST_HISTORIES,
    BACKTEST_HORIZONS,
    CONFIDENCES,
    WEEK,
    HowManyForecast,
    WhenForecast,
    backtest,
    how_many,
    score_backtest,
    to_utc,
    when,
)
from flowlens.storage import Store

THROUGHPUT_DROP = 0.2  # the "what if we lose an engineer" question
FORECAST_COUNTS = ("horizon_weeks", "history_weeks", "p50", "p85", "p95", "actual")
UNITS = {"issue": "issues", "pr": "PRs", "total": "items"}


@dataclass(frozen=True)
class Throughput:
    repo: str
    unit: str  # issue | pr | total
    weekly: pd.Series
    as_of: pd.Timestamp

    @property
    def next_start(self) -> pd.Timestamp:
        """The first Monday that hasn't started yet: where a forecast made now begins."""
        midnight = self.as_of.normalize()
        monday = midnight - pd.Timedelta(days=midnight.weekday())
        return monday if self.as_of == monday else monday + WEEK


def load_throughput(store: Store, repo: str, unit: str) -> Throughput:
    run = store.last_successful_run(repo)
    as_of = pd.Timestamp((run.finished_at if run else None) or datetime.now(UTC))
    items = flow.work_item_frame(store.rows("work_items"))
    floor = run.coverage_from if run else None
    if floor is None:
        floor = items["created_at"].min() if len(items) else as_of
    floor = pd.Timestamp(floor).normalize()
    first = floor + pd.Timedelta(days=(7 - floor.weekday()) % 7)  # Monday on or after
    end = flow.Window.ending(as_of, 1).end
    window = flow.Window(start=min(first, end), end=end, as_of=as_of)
    weekly = flow.weekly_throughput(items, window)[unit]
    return Throughput(repo=repo, unit=unit, weekly=weekly, as_of=as_of)


# --- forecast ---------------------------------------------------------------------------


def forecast_report(
    throughput: Throughput,
    *,
    items: int | None = None,
    by: datetime | None = None,
    start: datetime | None = None,
    history_weeks: int = 12,
    trials: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    """A "when" (``items``) or "how many" (``by``) forecast, with a 20% drop beside it."""
    if (items is None) == (by is None):
        raise ValueError("give exactly one of items or by")
    start = to_utc(start) if start is not None else throughput.next_start
    options = {"history_weeks": history_weeks, "trials": trials, "seed": seed}
    dropped = {**options, "throughput_factor": 1 - THROUGHPUT_DROP}
    if items is not None:
        base = when(throughput.weekly, items, start, **options)
        drop = when(throughput.weekly, items, start, **dropped)
    else:
        base = how_many(throughput.weekly, by, start, **options)
        drop = how_many(throughput.weekly, by, start, **dropped)
    history = throughput.weekly[throughput.weekly.index + WEEK <= start].iloc[-history_weeks:]
    report = {
        "repo": throughput.repo,
        "unit": throughput.unit,
        "as_of": _iso(throughput.as_of),
        "start": _iso(start),
        "trials": trials,
        "seed": seed,
        "history": {
            "weeks": history_weeks,
            "first_week": _iso(history.index[0]),
            "last_week": _iso(history.index[-1]),
            "weekly": [int(n) for n in history],
            "mean": float(history.mean()),
        },
    }
    if isinstance(base, WhenForecast):
        report["when"] = {
            "items": items,
            "dates": {str(c): _iso(d) for c, d in base.dates.items()},
            "weeks": {str(c): base.weeks_at(c) for c in CONFIDENCES},
            "distribution": [
                {"date": _iso(row.date), "share": row.share, "cumulative": row.cumulative}
                for row in base.distribution().itertuples()
            ],
        }
        report["sensitivity"] = {
            "throughput_drop": THROUGHPUT_DROP,
            "dates": {str(c): _iso(d) for c, d in drop.dates.items()},
            "p85_weeks_later": drop.weeks_at(85) - base.weeks_at(85),
        }
    else:
        report["how_many"] = _how_many_record(base, by)
        report["sensitivity"] = {
            "throughput_drop": THROUGHPUT_DROP,
            "at_least": {str(c): n for c, n in drop.counts.items()},
            "p85_fewer": base.at_least(85) - drop.at_least(85),
        }
    return report


def format_forecast(report: dict[str, Any]) -> str:
    r = report
    unit = UNITS[r["unit"]]
    start = r["start"][:10]
    history = r["history"]
    weekly = history["weekly"]
    setup = f"{history['weeks']} weeks of history, {r['trials']:,} trials"
    lines = []
    if "when" in r:
        lines.append(f"Forecast: {r['when']['items']:,} {unit} from {start} ({setup})")
    else:
        h = r["how_many"]
        lines.append(
            f"Forecast: {unit} done from {start} through {h['through'][:10]} "
            f"({h['weeks']} whole weeks; {setup})"
        )
    lines.append(
        f"  history: {history['first_week'][:10]} → {_sunday(history['last_week'])}, "
        f"{history['mean']:.1f} {unit}/week (min {min(weekly)}, max {max(weekly)}, "
        f"{weekly.count(0)} empty weeks)"
    )
    sensitivity = r["sensitivity"]
    drop = f"If throughput drops {sensitivity['throughput_drop']:.0%}"
    if "when" in r:
        dates = r["when"]["dates"]
        lines.append("  " + " | ".join(f"{c}%: by {dates[str(c)][:10]}" for c in CONFIDENCES))
        later = sensitivity["p85_weeks_later"]
        lines.append(
            f"  {drop}: 85% by {sensitivity['dates']['85'][:10]} ({_plural(later, 'week')} later)"
        )
    else:
        counts = r["how_many"]["at_least"]
        lines.append("  " + " | ".join(f"{c}%: at least {counts[str(c)]:,}" for c in CONFIDENCES))
        lines.append(
            f"  {drop}: 85%: at least {sensitivity['at_least']['85']:,} "
            f"({sensitivity['p85_fewer']:,} fewer)"
        )
    return "\n".join(lines)


def _how_many_record(forecast: HowManyForecast, by: datetime) -> dict[str, Any]:
    return {
        "by": _iso(to_utc(by)),
        "weeks": forecast.weeks,
        "through": _iso(forecast.through),
        "at_least": {str(c): n for c, n in forecast.counts.items()},
        "distribution": [
            {"items": int(row.items), "share": row.share, "at_least": row.at_least}
            for row in forecast.distribution().itertuples()
        ],
    }


# --- backtest ---------------------------------------------------------------------------


def backtest_report(
    throughput: Throughput, *, origins: int = 52, trials: int = 10_000, seed: int = 0
) -> dict[str, Any]:
    rows = backtest(throughput.weekly, origins=origins, trials=trials, seed=seed)
    scores = score_backtest(rows) if len(rows) else pd.DataFrame()
    return {
        "repo": throughput.repo,
        "unit": throughput.unit,
        "as_of": _iso(throughput.as_of),
        "weeks_available": len(throughput.weekly),
        "first_week": _iso(throughput.weekly.index[0]) if len(throughput.weekly) else None,
        "trials": trials,
        "seed": seed,
        "scores": [
            {
                "horizon_weeks": int(horizon),
                "history_weeks": int(history),
                "origins": int(row["origins"]),
                "first_origin": _iso(row["first_origin"]),
                "last_origin": _iso(row["last_origin"]),
                **{f"held_{c}": float(row[f"held_{c}"]) for c in CONFIDENCES},
                "mae": float(row["mae"]),
                "bias": float(row["bias"]),
                "mean_actual": float(row["mean_actual"]),
            }
            for (horizon, history), row in scores.iterrows()
        ],
        "forecasts": [
            {
                "origin": _iso(row["origin"]),
                **{key: int(row[key]) for key in FORECAST_COUNTS},
                **{f"held_{c}": bool(row[f"held_{c}"]) for c in CONFIDENCES},
            }
            for row in rows.to_dict("records")
        ],
    }


def format_backtest(report: dict[str, Any]) -> str:
    r = report
    unit = UNITS[r["unit"]]
    lines = [
        f"Backtest · {r['repo']} · {unit} · {r['trials']:,} trials per forecast "
        f"({r['weeks_available']} weeks of measured history from {(r['first_week'] or '-')[:10]})"
    ]
    if not r["scores"]:
        needed = max(BACKTEST_HISTORIES) + min(BACKTEST_HORIZONS)
        lines.append(
            f"  Not enough history to backtest: it needs at least {needed} measured weeks "
            "(the longest history plus the shortest horizon). Ingest further back with --since."
        )
        return "\n".join(lines)
    for horizon in sorted({s["horizon_weeks"] for s in r["scores"]}):
        scores = [s for s in r["scores"] if s["horizon_weeks"] == horizon]
        first = scores[0]
        lines += [
            "",
            f"{horizon}-week horizon: {first['origins']} weekly origins, "
            f"{first['first_origin'][:10]} → {first['last_origin'][:10]}, "
            f"{first['mean_actual']:.1f} {unit} delivered on average",
            f"  {'history':<10}{'50% held':>10}{'85% held':>10}{'95% held':>10}"
            f"{'p50 MAE':>10}{'p50 bias':>10}",
        ]
        for s in scores:
            held = "".join(f"{s[f'held_{c}']:>10.0%}" for c in CONFIDENCES)
            lines.append(
                f"  {s['history_weeks']:>2} weeks  {held}{s['mae']:>10.1f}{s['bias']:>+10.1f}"
            )
    lines += [
        "",
        "A forecast held when at least its count was delivered. Well calibrated means the 85%",
        "forecast holds about 85% of the time: higher is too cautious, lower is overconfident.",
        "Bias is actual minus p50; positive means the forecasts ran low.",
    ]
    return "\n".join(lines)


def _iso(value: Any) -> str | None:
    if value is None or value is pd.NaT:
        return None
    return pd.Timestamp(value).tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _sunday(monday_iso: str) -> str:
    return (pd.Timestamp(monday_iso) + pd.Timedelta(days=6)).strftime("%Y-%m-%d")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
