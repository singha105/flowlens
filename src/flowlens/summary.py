"""The words around the charts: stat tiles and the three-bullet executive summary.

Every sentence comes from the report dicts, and each judgment ("predictable", "waiting,
not building") is a named rule here, so the summary changes when the numbers do.
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd

from flowlens.charts import (
    MIN_BACKTEST_ITEMS,
    MIN_TRAILING_DEPLOYS,
    NOUNS,
    date_text,
    days_text,
    hours_text,
)
from flowlens.metrics.report import STAGE_NAMES

# An 85% forecast that held within this much of 85% is "close to its promise".
CALIBRATION_TOLERANCE = 0.05
WAITING_STAGES = frozenset({"waiting_for_review", "waiting_to_merge"})
BOTTLENECK_LEADS = {
    "waiting_for_review": "The bottleneck is waiting, not building",
    "waiting_to_merge": "The bottleneck is waiting, not building",
    "in_review": "The bottleneck is review itself",
    "coding": "The bottleneck is building",
}
# Below this share of a month's merged PRs with a human review, the summary says so.
FEW_REVIEWS = 0.5


@dataclass(frozen=True)
class Tile:
    label: str
    value: str
    note: str = ""
    band: str | None = None  # elite | high | medium | low
    source: str | None = None  # the report the band's limits come from


@dataclass(frozen=True)
class Bullet:
    lead: str
    text: str

    def __str__(self) -> str:
        return f"{self.lead}: {self.text}"


def executive_summary(
    flow: dict[str, Any],
    dora: dict[str, Any],
    backtest: dict[str, Any] | None,
    *,
    horizon: int = 4,
    history_weeks: int = 12,
) -> list[Bullet]:
    return [
        _delivery(flow, backtest, horizon, history_weeks),
        _bottleneck(flow),
        _dora(dora),
    ]


def _delivery(
    flow: dict[str, Any], backtest: dict[str, Any] | None, horizon: int, history_weeks: int
) -> Bullet:
    type_ = "pr" if flow["cycle_time_days"]["pr"]["all"]["count"] else "issue"
    plural = NOUNS[type_][1]
    stats = flow["cycle_time_days"][type_]["all"]
    if not stats["count"]:
        return Bullet("Delivery", "nothing was delivered in the window.")
    verb = "merge" if type_ == "pr" else "close"
    promise = f"85% of {plural} {verb} within {days_text(stats['p85'])} of starting"
    score = _score(backtest, horizon, history_weeks)
    if score is None:
        return Bullet("Delivery", f"{promise}; there's too little history to backtest forecasts.")
    unit = NOUNS[backtest["unit"]][0]
    held85, held95 = score["held_85"], score["held_95"]
    if score["mean_actual"] < MIN_BACKTEST_ITEMS:
        return Bullet(
            "Delivery",
            f"{promise}; too few {NOUNS[backtest['unit']][1]} are delivered "
            f"({score['mean_actual']:.1f} per {horizon} weeks) to judge forecasts.",
        )
    if held85 < 0.85 - CALIBRATION_TOLERANCE:
        return Bullet(
            f"{plural[:1].upper() + plural[1:]} are predictable, forecasts optimistic",
            f"{promise}, but 85% {unit} forecasts held only {held85:.0%} of the time in "
            f"backtests, so quote the 95% (held {held95:.0%}).",
        )
    lead = "Delivery is predictable"
    if held85 > 0.85 + CALIBRATION_TOLERANCE:
        lead += ", forecasts cautious"
    return Bullet(
        lead, f"{promise}, and 85% {unit} forecasts held {held85:.0%} of the time in backtests."
    )


def _bottleneck(flow: dict[str, Any]) -> Bullet:
    pr_flow = flow["pr_flow"]
    stage = pr_flow["bottleneck"]
    if stage is None:
        return Bullet("PR flow", "no PR stage stands out; too few PRs had every timestamp.")
    if pr_flow["merged"] and not pr_flow["stages"]["waiting_for_review"]["count"]:
        return Bullet(
            "PR flow",
            f"none of the {pr_flow['merged']:,} merged PRs had a human review, so there's no "
            "review wait to measure.",
        )
    hours = pr_flow["stages"][stage]["median_hours"]
    text = (
        f"{STAGE_NAMES[stage]} takes a median of {hours_text(hours)}, "
        f"{pr_flow['bottleneck_share']:.0%} of the summed PR stage medians."
    )
    monthly = pr_flow["monthly"]
    if monthly and (monthly[-1]["reviewed_share"] or 0) < FEW_REVIEWS:
        month = pd.Timestamp(monthly[-1]["month"] + "-01")
        # "Getting rare" only when an earlier month had them; otherwise they always were.
        trend = (
            "getting rare"
            if any((m["reviewed_share"] or 0) >= FEW_REVIEWS for m in monthly[:-1])
            else "rare"
        )
        text += (
            f" Human reviews are {trend}: {monthly[-1]['reviewed_share']:.0%} of "
            f"{month:%B}'s merged PRs had one."
        )
    return Bullet(BOTTLENECK_LEADS[stage], text)


def _dora(dora: dict[str, Any]) -> Bullet:
    m = dora["metrics"]
    freq, lead, cfr = m["deployment_frequency"], m["lead_time"], m["change_failure_rate"]
    if not cfr["deploys"]:
        return Bullet("DORA", f"no deploys from {dora['deployment_source']} in the window.")
    band = f"{freq['band']} " if freq["band"] else ""
    text = f"{band}deployment frequency ({freq['per_week']:.2f} a week)"
    share = lead["waiting_share"]
    if share is not None:
        joiner = "but" if share >= 0.5 else "and"
        text += f", {joiner} {share:.0%} of lead time is waiting for a release"
    window = f"{cfr['failure_window_hours']:.0f} hours"
    if cfr["deploys"] < MIN_TRAILING_DEPLOYS:
        text += (
            f"; {cfr['failed']} of {cfr['deploys']} deploys failed within {window}, too few "
            "deploys to rate."
        )
    else:
        failure_band = f" ({cfr['band']} band)" if cfr["band"] else ""
        text += f"; {cfr['rate']:.0%} of deploys failed within {window}{failure_band}."
    return Bullet("DORA", text)


def _score(backtest: dict[str, Any] | None, horizon: int, history_weeks: int) -> dict | None:
    for score in (backtest or {}).get("scores", []):
        if score["horizon_weeks"] == horizon and score["history_weeks"] == history_weeks:
            return score
    return None


# --- tiles ------------------------------------------------------------------------------


def flow_tiles(flow: dict[str, Any], type_: str) -> list[Tile]:
    singular, plural = NOUNS[type_]
    cycle = flow["cycle_time_days"][type_]["all"]
    throughput = flow["throughput"]["summary"][type_]
    aging = flow["aging_wip"]
    in_progress = sum(1 for item in aging["items"] if item["type"] == type_)
    old = sum(1 for item in aging["items"] if item["type"] == type_ and item["over_p85"])
    rate = throughput["mean"]
    return [
        Tile(
            f"{_cap(singular)} cycle time p85",
            days_text(cycle["p85"]),
            f"p50 {days_text(cycle['p50'])}, over {cycle['count']:,} finished {plural}",
        ),
        Tile(
            f"{_cap(singular)} throughput",
            f"{rate:.1f} a week" if rate is not None else "-",
            f"{throughput['min']} to {throughput['max']} {plural} in a week"
            if rate is not None
            else "",
        ),
        Tile(
            f"{_cap(plural)} in progress",
            f"{in_progress:,}",
            f"{old:,} older than the p85 cycle time",
        ),
    ]


def forecast_tiles(when: dict[str, Any] | None, how_many: dict[str, Any] | None) -> list[Tile]:
    tiles = []
    if when is not None:
        unit = NOUNS[when["unit"]][1]
        dates = when["when"]["dates"]
        later = when["sensitivity"]["p85_weeks_later"]
        tiles.append(
            Tile(
                f"{when['when']['items']:,} {unit} done, 85% sure",
                f"by {date_text(pd.Timestamp(dates['85']))}",
                f"95%: by {date_text(pd.Timestamp(dates['95']))}; "
                f"{later} week{'s' if later != 1 else ''} later if throughput drops 20%",
            )
        )
    if how_many is not None:
        unit = NOUNS[how_many["unit"]][1]
        h = how_many["how_many"]
        tiles.append(
            Tile(
                f"{_cap(unit)} by {date_text(pd.Timestamp(h['through']))}, 85% sure",
                f"at least {h['at_least']['85']:,}",
                f"95%: at least {h['at_least']['95']:,}; "
                f"{how_many['sensitivity']['p85_fewer']:,} fewer if throughput drops 20%",
            )
        )
    return tiles


def dora_tiles(dora: dict[str, Any]) -> list[Tile]:
    m = dora["metrics"]
    bands = dora["bands"]
    freq, lead = m["deployment_frequency"], m["lead_time"]
    cfr, recovery = m["change_failure_rate"], m["recovery_time"]
    source = {key: band["source"] for key, band in bands.items()}
    lead_value = days_text(lead["p50_days"]) if lead["changes"] else "-"
    lead_note = (
        f"p85 {days_text(lead['p85_days'])}; coding {days_text(lead['coding_p50_days'])}, "
        f"waiting {days_text(lead['waiting_p50_days'])} (p50s)"
        if lead["changes"]
        else "no PRs shipped in the window"
    )
    rate = f"{cfr['rate']:.0%}" if cfr["rate"] is not None else "-"
    open_note = f", {recovery['open']} still open" if recovery["open"] else ""
    return [
        Tile(
            "Deployment frequency",
            f"{freq['per_week']:.2f} a week",
            f"{freq['deploys']} deploys; median gap {days_text(freq['median_gap_days'])}",
            freq["band"],
            source.get("deployments_per_week"),
        ),
        Tile(
            "Lead time for changes (p50)",
            lead_value,
            lead_note,
            lead["band"],
            source.get("lead_time_hours"),
        ),
        Tile(
            "Change failure rate",
            rate,
            f"{cfr['failed']} of {cfr['deploys']} deploys, within "
            f"{cfr['failure_window_hours']:.0f}h",
            cfr["band"],
            source.get("change_failure_rate"),
        ),
        Tile(
            "Recovery time (median)",
            hours_text(recovery["median_hours"]),
            f"{recovery['recovered']} of {recovery['failed']} failed deploys recovered{open_note}",
            recovery["band"],
            source.get("recovery_hours"),
        ),
    ]


def pr_tiles(flow: dict[str, Any]) -> list[Tile]:
    pr_flow = flow["pr_flow"]
    stage = pr_flow["bottleneck"]
    reviewed = pr_flow["stages"]["waiting_for_review"]["count"]
    tiles = [
        Tile("PRs merged", f"{pr_flow['merged']:,}", "in the window, bots excluded"),
        Tile(
            "Human-reviewed",
            f"{reviewed / pr_flow['merged']:.0%}" if pr_flow["merged"] else "-",
            f"{reviewed:,} PRs had a review before merging",
        ),
    ]
    if stage:
        tiles.insert(
            1,
            Tile(
                "Bottleneck",
                hours_text(pr_flow["stages"][stage]["median_hours"]),
                f"{STAGE_NAMES[stage]}, {pr_flow['bottleneck_share']:.0%} of the stage medians",
            ),
        )
    return tiles


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]
