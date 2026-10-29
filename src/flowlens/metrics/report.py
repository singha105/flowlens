"""The ``flowlens flow`` report: every flow metric for one repo, as JSON and as a table.

`flow_report` turns the pure metrics in flowlens.metrics.flow into one JSON-ready dict (the
dashboard reads it from data/out/flow_<owner>__<name>.json); `format_flow` prints the
compact summary. docs/METRICS.md defines each metric.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from flowlens.config import LABEL_KINDS
from flowlens.metrics import flow
from flowlens.metrics.flow import ALL, OTHER, PR_STAGES, TYPES, Window
from flowlens.storage import Store

TYPE_NAMES = {"issue": "issues", "pr": "PRs"}
STAGE_ARROWS = {
    "coding": "first commit→open",
    "waiting_for_review": "open→first review",
    "in_review": "review→approve",
    "waiting_to_merge": "approve→merge",
}
STAGE_NAMES = {
    "coding": "coding before the PR opened",
    "waiting_for_review": "waiting for first review",
    "in_review": "in review until approved",
    "waiting_to_merge": "waiting to merge after approval",
}


def load_frames(store: Store) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The model's work items and the PR timestamps, as typed DataFrames."""
    items = flow.work_item_frame(store.rows("work_items"))
    prs = flow.pull_request_frame(store.rows("pull_requests"))
    return items, prs


def flow_report_for(store: Store, repo: str, *, weeks: int) -> dict[str, Any]:
    """The report as of the last successful ingest (or now, if there wasn't one)."""
    run = store.last_successful_run(repo)
    as_of = (run.finished_at if run else None) or datetime.now(UTC)
    items, prs = load_frames(store)
    return flow_report(
        items,
        prs,
        repo=repo,
        window=Window.ending(as_of, weeks),
        history_from=run.coverage_from if run else None,
    )


def flow_report(
    items: pd.DataFrame,
    prs: pd.DataFrame,
    *,
    repo: str,
    window: Window,
    history_from: datetime | None = None,
) -> dict[str, Any]:
    durations = flow.durations(items, window)
    lead = flow.time_summary(durations, "lead_days")
    cycle = flow.time_summary(durations, "cycle_days")
    weekly = flow.weekly_throughput(items, window)
    wip = flow.daily_wip(items, window)
    cfd = {type_: flow.cumulative_flow(items, window, type_) for type_ in TYPES}
    cfd["total"] = flow.cumulative_flow(items, window)
    aging = flow.aging_wip(items, window.as_of, {t: cycle[t][ALL] for t in TYPES})
    stages = flow.pr_stages(items, prs, window)
    pr_flow = flow.pr_flow(stages)
    coverage = {
        type_: {
            "delivered": lead[type_][ALL]["count"],
            "with_start": cycle[type_][ALL]["count"],
        }
        for type_ in TYPES
    }

    report = {
        "repo": repo,
        "as_of": _iso(window.as_of),
        "window": {
            "weeks": window.weeks,
            "start": _iso(window.start),
            "end": _iso(window.end),  # exclusive
            "history_from": _iso(history_from),
        },
        "coverage": coverage,
        "lead_time_days": lead,
        "cycle_time_days": cycle,
        "throughput": {
            "weekly": [
                {"week": _iso(week), "iso_week": _iso_week(week), **_ints(row)}
                for week, row in weekly.iterrows()
            ],
            "summary": {
                column: flow.throughput_summary(weekly[column]) for column in weekly.columns
            },
        },
        "wip": {
            "today": _ints(wip.iloc[-1]) if len(wip) else dict.fromkeys(wip.columns, 0),
            "daily": [{"day": _iso(day), **_ints(row)} for day, row in wip.iterrows()],
        },
        "cumulative_flow": {
            name: [{"day": _iso(day), **_ints(row)} for day, row in frame.iterrows()]
            for name, frame in cfd.items()
        },
        "aging_wip": {
            "over_p85": int(aging["over_p85"].sum()),
            "items": [_aging_record(row) for row in aging.to_dict("records")],
        },
        "pr_flow": {
            **pr_flow,
            "monthly": [
                {key: _number(value) if key != "month" else value for key, value in row.items()}
                for row in flow.pr_flow_by_month(stages).reset_index().to_dict("records")
            ],
        },
    }
    report["notes"] = notes(report, window, history_from)
    return report


def notes(report: dict, window: Window, history_from: datetime | None) -> list[str]:
    """Caveats a reader needs before trusting the numbers."""
    out = []
    issues = report["coverage"]["issue"]
    if issues["delivered"] and issues["with_start"] < issues["delivered"]:
        out.append(
            f"Issue cycle time covers {issues['with_start']:,} of {issues['delivered']:,} "
            f"issues delivered ({_pct(issues['with_start'], issues['delivered'])}); the rest "
            "closed with no start signal (no assignment, linked PR or in-progress label)."
        )
    pr_flow = report["pr_flow"]
    reviewed = pr_flow["stages"]["waiting_for_review"]["count"]
    if pr_flow["merged"] and reviewed < pr_flow["merged"]:
        out.append(
            f"{pr_flow['merged'] - reviewed:,} of {pr_flow['merged']:,} merged PRs had no human "
            "review before merging; they're left out of the review stages."
        )
    if history_from is not None:
        if history_from > window.start:
            out.append(
                f"The window starts {window.start:%Y-%m-%d} but history was ingested from "
                f"{history_from:%Y-%m-%d}; early weeks undercount."
            )
        out.append(
            f"Items not updated since {history_from:%Y-%m-%d} aren't in the data, so "
            "long-idle open items are missing from WIP and aging."
        )
    return out


def write_report(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return path


def format_flow(report: dict[str, Any]) -> str:
    r = report
    window = r["window"]
    start = window["start"][:10]
    last_day = (pd.Timestamp(window["end"]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    as_of = pd.Timestamp(r["as_of"]).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"FlowLens flow · {r['repo']} · {window['weeks']} weeks, {start} → {last_day} "
        f"(as of {as_of} UTC)",
        "",
        f"{'':26}{'n':>6}{'p50':>8}{'p85':>8}{'p95':>8}",
    ]
    for title, key in (("Cycle time", "cycle_time_days"), ("Lead time", "lead_time_days")):
        for i, type_ in enumerate(TYPES):
            groups = r[key][type_]
            label = title if i == 0 else ""
            lines.append(_percentile_row(f"{label:<12}{TYPE_NAMES[type_]}", groups[ALL]))
            kinds = [k for k in LABEL_KINDS if groups[k]["count"]]
            for group in [*kinds, OTHER] if kinds else []:
                lines.append(_percentile_row(f"{'':14}{group}", groups[group]))
    lines.append("")

    throughput = r["throughput"]["summary"]
    parts = []
    for type_ in TYPES:
        s = throughput[type_]
        if s["mean"] is not None:
            parts.append(
                f"{TYPE_NAMES[type_]} {s['mean']:.1f}/week (min {s['min']}, max {s['max']})"
            )
    zero = throughput["total"]["zero_weeks"]
    lines.append(f"{'Throughput':<12}{' | '.join(parts)} | weeks with none: {zero}")

    today = r["wip"]["today"]
    aging = r["aging_wip"]
    wip = f"{today['total']} ({TYPE_NAMES['issue']} {today['issue']}, PRs {today['pr']})"
    if aging["items"]:
        oldest = aging["items"][0]
        wip += (
            f" | aging > p85: {aging['over_p85']} item{'' if aging['over_p85'] == 1 else 's'} "
            f"(oldest {oldest['type'].replace('pr', 'PR')} #{oldest['number']}, "
            f"{oldest['age_days']:.0f} days)"
        )
    lines.append(f"{'WIP today':<12}{wip}")

    pr_flow = r["pr_flow"]
    medians = " | ".join(
        f"{STAGE_ARROWS[stage]} {_hours(pr_flow['stages'][stage]['median_hours'])}"
        for stage, *_ in PR_STAGES
    )
    lines.append(f"{'PR flow':<12}medians: {medians}")
    if pr_flow["bottleneck"]:
        lines.append(
            f"{'Bottleneck':<12}{STAGE_NAMES[pr_flow['bottleneck']]} "
            f"({pr_flow['bottleneck_share']:.0%} of the summed stage medians)"
        )
    if r["notes"]:
        lines.append("")
        lines += [f"  ! {note}" for note in r["notes"]]
    return "\n".join(lines)


def _percentile_row(label: str, stats: dict) -> str:
    values = "".join(f"{_days(stats[f'p{p}']):>8}" for p in flow.PERCENTILES)
    return f"{label:<26}{stats['count']:>6,}{values}"


def _days(days: float | None) -> str:
    if days is None:
        return "-"
    if days < 1:
        return _hours(days * 24)
    return f"{days:.1f}d"


def _hours(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "n/a"


def _iso(value: Any) -> str | None:
    if value is None or value is pd.NaT:
        return None
    return pd.Timestamp(value).tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _iso_week(week: pd.Timestamp) -> str:
    year, number, _ = week.isocalendar()
    return f"{year}-W{number:02d}"


def _ints(row: pd.Series) -> dict[str, int]:
    return {str(key): int(value) for key, value in row.items()}


def _number(value: Any) -> float | int | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, int | np.integer):
        return int(value)
    return float(value)


def _aging_record(row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "number": int(row["number"]),
        "title": row["title"],
        "url": row["url"],
        "started_at": _iso(row["started_at"]),
        "age_days": float(row["age_days"]),
        "cycle_p50": _number(row["cycle_p50"]),
        "cycle_p85": _number(row["cycle_p85"]),
        "over_p50": bool(row["over_p50"]),
        "over_p85": bool(row["over_p85"]),
    }
