"""Charts: each draws the report's own numbers, says so in its caption, and themes cleanly."""

import json
import re

import numpy as np
import pandas as pd
import plotly.io as pio
import pytest

from flowlens import charts
from flowlens.charts import (
    BLUE,
    CONTEXT,
    CRITICAL,
    GOOD,
    MUTED,
    SERIOUS,
    WARNING,
    days_text,
    hours_text,
)
from flowlens.metrics.dora_report import failure_rows
from flowlens.metrics.forecast_report import Throughput, forecast_report
from repos import empty_repo, synthetic_repo

COLOR = re.compile(r"^(#[0-9a-f]{6}|rgba?\([^)]*\))$")
INVARIANT = {MUTED, GOOD, WARNING, SERIOUS, CRITICAL, "rgba(0,0,0,0)"}


@pytest.fixture(scope="module")
def views():
    data = synthetic_repo()
    window = data.window(26)
    return data.flow(window), data.finished(window), data.dora(window)


def trace(chart: charts.Chart, name: str):
    (found,) = [t for t in chart.figure.data if t.name and t.name.startswith(name)]
    return found


# --- flow -------------------------------------------------------------------------------


def test_the_cycle_time_scatter_draws_each_finished_item_under_its_percentiles(views):
    flow, finished, _ = views
    stats = flow["cycle_time_days"]["pr"]["all"]

    chart = charts.cycle_time_scatter(finished, flow, "pr")

    dots = trace(chart, "Finished PRs")
    assert len(dots.y) == stats["count"]
    real = np.array([float(row[2]) for row in dots.customdata])  # hover shows the real value
    assert (real <= stats["p85"]).mean() >= 0.85
    assert trace(chart, "p85").y == (stats["p85"], stats["p85"])
    assert f"{days_text(stats['p85'])} is a fair delivery promise for a PR" in chart.caption
    assert chart.figure.layout.yaxis.type == "log"


def test_throughput_stacks_each_weeks_prs_and_issues(views):
    flow, _, _ = views
    weekly = flow["throughput"]["weekly"]

    chart = charts.throughput_chart(flow)

    assert list(trace(chart, "PRs").y) == [w["pr"] for w in weekly]
    assert list(trace(chart, "Issues").y) == [w["issue"] for w in weekly]
    assert chart.figure.layout.barmode == "stack"
    mean = flow["throughput"]["summary"]["pr"]["mean"]
    assert f"{mean:.0f} PRs" in chart.caption


def test_cumulative_flow_stacks_done_wip_and_to_do_in_that_order(views):
    flow, _, _ = views
    days = flow["cumulative_flow"]["issue"]

    chart = charts.cumulative_flow_chart(flow, "issue")

    assert [t.name for t in chart.figure.data] == ["Done", "In progress (WIP)", "To do"]
    assert list(chart.figure.data[1].y) == [d["in_progress"] for d in days]
    assert f"{days[-1]['in_progress']:,} at the end" in chart.caption


def test_aging_colors_items_by_how_far_past_the_cycle_time_they_are(views):
    flow, _, _ = views
    aging = flow["aging_wip"]

    chart = charts.aging_wip_chart(flow)

    assert len(trace(chart, "Older than p85").y) == aging["over_p85"]
    assert trace(chart, "Older than p85").marker.color == CRITICAL
    dots = [t for t in chart.figure.data if t.mode == "markers"]
    assert sum(len(t.y) for t in dots) == len(aging["items"])
    assert f"{aging['over_p85']:,} of {len(aging['items']):,}" in chart.caption


# --- forecast ---------------------------------------------------------------------------


def steady(per_week: int, weeks: int = 12) -> Throughput:
    mondays = pd.date_range("2026-08-03", periods=weeks, freq="7D", tz="UTC")
    return Throughput(
        "o/r", "pr", pd.Series(per_week, index=mondays), mondays[-1] + pd.Timedelta(days=8)
    )


def test_confidence_levels_on_the_same_date_share_one_line():
    report = forecast_report(steady(5), items=40, trials=500)  # exactly 8 weeks, every trial

    chart = charts.when_chart(report)

    (line,) = [t for t in chart.figure.data if t.type == "scatter"]
    assert line.name == "50%, 85% and 95%: by Dec 28, 2026"
    assert "85% finish by Dec 28, 2026" in chart.caption


def test_how_many_marks_the_counts_a_plan_can_commit_to():
    report = forecast_report(steady(5), by=pd.Timestamp("2026-12-27", tz="UTC"), trials=500)

    chart = charts.how_many_chart(report)

    assert [t.name for t in chart.figure.data if t.type == "scatter"] == [
        "50%, 85% and 95%: at least 40"
    ]
    assert "85% deliver at least 40" in chart.caption


def backtest(held: dict[int, tuple[float, float, float]]) -> dict:
    return {
        "unit": "issue",
        "scores": [
            {"horizon_weeks": 4, "history_weeks": h, "origins": 52, "first_origin": "x",
             "last_origin": "y", "held_50": a, "held_85": b, "held_95": c, "mae": 1.0,
             "bias": 0.0, "mean_actual": 10.0}
            for h, (a, b, c) in held.items()
        ],
    }  # fmt: skip


@pytest.mark.parametrize(
    ("held85", "advice"),
    [
        (0.71, "so quote the 95% forecast (held 85%)"),
        (0.84, "close to its promise"),
        (0.95, "a little cautious"),
    ],
)
def test_calibration_highlights_the_history_in_use_and_says_what_to_quote(held85, advice):
    report = backtest({6: (0.5, 0.6, 0.7), 12: (0.56, held85, 0.85), 26: (0.6, 0.8, 0.9)})

    chart = charts.calibration_chart(report, horizon=4, history_weeks=12)

    assert trace(chart, "12 weeks").line.color == charts.ORANGE  # issues
    assert trace(chart, "6 weeks").line.color in CONTEXT
    assert list(trace(chart, "12 weeks").y) == pytest.approx([56, held85 * 100, 85])
    assert f"held {held85:.0%} of the time, {advice}." in chart.caption


def test_calibration_is_not_judged_on_a_trickle_of_deliveries():
    report = backtest({12: (1.0, 1.0, 1.0)})
    report["scores"][0]["mean_actual"] = 0.6

    chart = charts.calibration_chart(report, horizon=4, history_weeks=12)

    assert chart.caption.endswith(
        "the 85% forecast held 100% of the time, but only 0.6 issues were delivered per 4 "
        "weeks on average: too few to judge the forecasts."
    )


# --- DORA -------------------------------------------------------------------------------


def test_the_dora_charts_draw_the_reports_deploys_and_failures(views):
    _, _, dora = views  # v1 and v2 failed, both recovered: 20h and 30h

    deploys = charts.deploys_chart(dora)
    recovery = charts.recovery_chart(dora)
    lead = charts.lead_time_trend(dora)

    assert sum(trace(deploys, "Failed").y) == 2 and sum(trace(deploys, "Held").y) == 2
    assert list(trace(recovery, "Recovered").x) == ["v1", "v2"]
    assert list(trace(recovery, "Recovered").y) == pytest.approx([20 / 24, 30 / 24])
    assert trace(recovery, "Median").name == "Median 20 hours"
    assert len(trace(lead, "p50 lead time").y) == 3  # v1, v2, v3 shipped PRs; v4 none
    assert "2 of 4 deploys over the window" in charts.failure_rate_trend(dora).caption


def test_the_trailing_failure_rate_waits_for_enough_deploys(views):
    _, _, dora = views

    chart = charts.failure_rate_trend(dora, trailing=8, min_deploys=3)

    rates = trace(chart, "Trailing").y
    weeks_with_three = [row["deploys"] >= 3 for row in chart.table.to_dict("records")]
    assert [r is not None and not np.isnan(r) for r in rates] == weeks_with_three


def test_the_failed_deployments_table_has_a_row_per_signal(views):
    _, _, dora = views

    table = charts.failed_deploys_table(failure_rows(dora))

    assert list(table["Deploy"]) == ["v1", "v2"]
    assert list(table["Signal"]) == ["PR #40", "PR #41"]
    assert list(table["Recovered"]) == ["20 hours", "30 hours"]


# --- PR flow ----------------------------------------------------------------------------


def test_pr_flow_highlights_the_bottleneck_against_gray(views):
    flow, _, _ = views
    bottleneck = flow["pr_flow"]["bottleneck"]

    chart = charts.pr_flow_chart(flow)

    colors = {t.name: t.marker.color for t in chart.figure.data}
    highlighted = [name for name, color in colors.items() if color == BLUE]
    assert len(highlighted) == 1 and highlighted[0].endswith("(bottleneck)")
    assert {c for c in colors.values() if c != BLUE} <= set(CONTEXT)
    assert f"{flow['pr_flow']['bottleneck_share']:.0%} of the summed stage medians" in (
        chart.caption
    )
    assert bottleneck == "waiting_for_review"  # the synthetic team's slowest stage


# --- empty windows and themes -----------------------------------------------------------


def all_charts(data) -> list[charts.Chart]:
    window = data.window(4)
    flow, finished, dora = data.flow(window), data.finished(window), data.dora(window)
    return [
        charts.cycle_time_scatter(finished, flow, "pr"),
        charts.cycle_time_scatter(finished, flow, "issue"),
        charts.throughput_chart(flow),
        charts.cumulative_flow_chart(flow, "pr"),
        charts.aging_wip_chart(flow),
        charts.deploys_chart(dora),
        charts.lead_time_trend(dora),
        charts.failure_rate_trend(dora),
        charts.recovery_chart(dora),
        charts.pr_flow_chart(flow),
    ]


def test_an_empty_window_says_so_instead_of_drawing_nothing():
    built = all_charts(empty_repo())

    captions = {chart.key: chart.caption for chart in built}
    assert captions["cycle_pr"] == "No PRs with a start signal finished in this window."
    assert captions["aging"] == "Nothing is in progress."
    assert captions["recovery"] == "No failed deploys in this window."
    assert captions["pr_flow"] == "No PRs merged in this window."


def colors_in(value) -> set[str]:
    if isinstance(value, str):
        return {value} if COLOR.match(value) else set()
    if isinstance(value, dict):
        return set().union(*(colors_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(colors_in(v) for v in value)) if value else set()
    return set()


def test_every_color_the_figures_use_has_a_dark_mode_step():
    report = forecast_report(steady(5), items=40, trials=200)
    built = [*all_charts(synthetic_repo()), charts.when_chart(report)]
    built.append(charts.calibration_chart(backtest({12: (0.5, 0.8, 0.9)}), 4))

    used = set().union(*(colors_in(json.loads(pio.to_json(c.figure))) for c in built))

    dark = charts.dark_colors()
    assert used - INVARIANT - dark.keys() == set()
    assert dark[charts.SURFACE] == "#1a1a19"


@pytest.mark.parametrize(
    ("hours", "text"),
    [
        (0.004, "under a minute"),
        (0.014, "1 minute"),
        (0.25, "15 minutes"),
        (1, "1 hour"),
        (20, "20 hours"),
        (60, "2.5 days"),
        (2400, "100 days"),
    ],
)
def test_durations_read_in_the_unit_people_use(hours, text):
    assert hours_text(hours) == text
    assert days_text(None) == "-"
