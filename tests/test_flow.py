"""Flow metrics against ground truth.

Three kinds of check:

- exact: every metric on a seeded synthetic history equals a brute-force count over the
  same items (tests/synthetic.py), day by day and week by week;
- statistical: on a large history, the metrics recover the distributions the generator
  drew from (percentiles, arrival rates, Little's law, PR stage medians);
- edge cases on hand-built items: nothing at all, nothing finished, same-day finishes,
  empty weeks, window boundaries, exclusions, abandoned PRs.
"""

import statistics
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from flowlens.metrics import flow
from flowlens.metrics.flow import HISTOGRAM_BINS, HISTOGRAM_EDGES, Window
from synthetic import Scenario, Synthetic, days, generate, nearest_rank, work_item

DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)
MONDAY = datetime(2026, 3, 2, tzinfo=UTC)


def at(day: float, hour: float = 0) -> datetime:
    """A time ``day`` days and ``hour`` hours after MONDAY."""
    return MONDAY + timedelta(days=day, hours=hour)


def frame(*items) -> pd.DataFrame:
    return Synthetic(Scenario(), list(items)).frame()


def no_prs() -> pd.DataFrame:
    return flow.pull_request_frame([])


# --- the window -------------------------------------------------------------------------


def test_window_is_whole_iso_weeks_ending_the_monday_before_as_of():
    window = Window.ending(datetime(2026, 10, 29, 5, tzinfo=UTC), 26)  # a Thursday

    assert window.end == pd.Timestamp("2026-10-26", tz="UTC")
    assert window.start == pd.Timestamp("2026-04-27", tz="UTC")
    assert window.weeks == 26
    weeks = window.week_starts()
    assert len(weeks) == 26 and set(weeks.day_of_week) == {0}
    days_ = window.days()
    assert (days_[0], days_[-1]) == (window.start, pd.Timestamp("2026-10-29", tz="UTC"))


def test_a_window_ending_on_monday_midnight_has_no_partial_week():
    window = Window.ending(datetime(2026, 10, 26, tzinfo=UTC), 1)

    assert (window.start, window.end) == (pd.Timestamp("2026-10-19", tz="UTC"), window.as_of)
    assert window.days()[-1] == pd.Timestamp("2026-10-25", tz="UTC")


def test_window_rejects_naive_times_and_empty_windows():
    with pytest.raises(ValueError, match="naive"):
        Window.ending(datetime(2026, 10, 29), 4)
    with pytest.raises(ValueError, match="at least 1"):
        Window.ending(datetime(2026, 10, 29, tzinfo=UTC), 0)


# --- exact ground truth on a seeded history ---------------------------------------------

# 185 days from a Monday: as_of is a Thursday, so the window has a partial week after it.
SMALL = Scenario(days=185)


@pytest.fixture(scope="module")
def history() -> Synthetic:
    return generate(seed=3, scenario=SMALL)


@pytest.fixture(scope="module")
def window(history) -> Window:
    return Window.ending(history.as_of, 20)


@pytest.fixture(scope="module")
def items(history) -> pd.DataFrame:
    return history.frame()


def test_the_history_has_every_kind_of_item(history, window):
    outcomes = {(i.type, i.outcome) for i in history.items}
    assert outcomes >= {
        ("issue", "completed"),
        ("issue", "not_planned"),
        ("issue", "open"),
        ("pr", "merged"),
        ("pr", "abandoned"),
        ("pr", "open"),
    }
    assert any(i.excluded for i in history.items if i.type == "pr")  # bots
    assert any(i.started_at is None and i.done_at for i in history.items)  # never started
    assert window.end < pd.Timestamp(history.as_of)  # a partial week


@pytest.mark.parametrize("type_", ["issue", "pr"])
@pytest.mark.parametrize("measure", ["lead", "cycle"])
def test_lead_and_cycle_percentiles_are_nearest_rank(history, window, items, type_, measure):
    start, end = window.start.to_pydatetime(), window.end.to_pydatetime()
    oracle = getattr(history, f"{measure}_days")
    summary = flow.time_summary(flow.durations(items, window), f"{measure}_days")[type_]

    expected = oracle(start, end, type_)
    assert summary["all"]["count"] == len(expected) > 0
    for p in (50, 85, 95):
        assert summary["all"][f"p{p}"] == pytest.approx(nearest_rank(expected, p))


def test_percentiles_per_label_group(history, window, items):
    start, end = window.start.to_pydatetime(), window.end.to_pydatetime()
    summary = flow.time_summary(flow.durations(items, window), "cycle_days")["issue"]

    by_kind = {"bug": [], "other": []}
    for i in history.done_between(start, end, "issue"):
        if i.started_at is not None:
            by_kind["bug" if "bug" in i.kinds else "other"].append(days(i.done_at - i.started_at))
    for group, values in by_kind.items():
        assert summary[group]["count"] == len(values) > 0
        assert summary[group]["p85"] == pytest.approx(nearest_rank(values, 85))
    assert summary["incident"] == flow.summarize([])


def test_histogram_counts_every_duration_once(history, window, items):
    start, end = window.start.to_pydatetime(), window.end.to_pydatetime()
    values = history.cycle_days(start, end, "issue")
    summary = flow.time_summary(flow.durations(items, window), "cycle_days")["issue"]["all"]

    expected = {label: 0 for label in HISTOGRAM_BINS}
    for value in values:
        for label, low, high in zip(
            HISTOGRAM_BINS, HISTOGRAM_EDGES, HISTOGRAM_EDGES[1:], strict=False
        ):
            if low <= value < high:
                expected[label] += 1
    assert summary["histogram"] == expected
    assert sum(expected.values()) == len(values)


def test_weekly_throughput_counts_each_week(history, window, items):
    weekly = flow.weekly_throughput(items, window)

    assert len(weekly) == window.weeks
    for week, row in weekly.iterrows():
        start = week.to_pydatetime()
        for type_ in ("issue", "pr"):
            assert row[type_] == len(history.done_between(start, start + WEEK, type_)), week
        assert row["total"] == row["issue"] + row["pr"]


def test_daily_wip_matches_a_brute_force_count(history, window, items):
    wip = flow.daily_wip(items, window)

    assert len(wip) == len(window.days())
    for day, row in wip.iterrows():
        cut = min(day + pd.Timedelta(days=1), window.as_of).to_pydatetime()
        assert row["issue"] == history.wip_at(cut, "issue"), day
        assert row["pr"] == history.wip_at(cut, "pr"), day
        assert row["total"] == history.wip_at(cut), day


@pytest.mark.parametrize("type_", ["issue", "pr", None])
def test_cumulative_flow_matches_a_brute_force_count(history, window, items, type_):
    cfd = flow.cumulative_flow(items, window, type_)
    start = window.start.to_pydatetime()

    for day, row in cfd.iterrows():
        cut = min(day + pd.Timedelta(days=1), window.as_of).to_pydatetime()
        assert row["todo"] == history.todo_at(cut, type_), day
        assert row["in_progress"] == history.wip_at(cut, type_), day
        assert row["done"] == len(history.done_between(start, cut, type_)), day


def test_aging_wip_lists_every_item_in_progress_oldest_first(history, window, items):
    cycle = {"issue": {"p50": 3.0, "p85": 9.0}, "pr": {"p50": 1.0, "p85": 2.0}}
    aging = flow.aging_wip(items, window.as_of, cycle)

    as_of = history.as_of
    expected = {
        i.id: days(as_of - i.started_at)
        for i in history.counted()
        if i.started_at is not None
        and i.started_at < as_of
        and (i.done_at is None or i.done_at >= as_of)
    }
    assert dict(zip(aging["id"], aging["age_days"], strict=True)) == pytest.approx(expected)
    assert list(aging["age_days"]) == sorted(aging["age_days"], reverse=True)
    for row in aging.itertuples():
        assert row.over_p85 == (row.age_days > cycle[row.type]["p85"])
        assert row.over_p50 == (row.age_days > cycle[row.type]["p50"])
    assert len(aging) == flow.daily_wip(items, window)["total"].iloc[-1]


def test_pr_stage_medians_match_a_brute_force_median(history, window, items):
    stages = flow.pr_stages(items, history.pr_frame(), window)
    summary = flow.pr_flow(stages)

    expected = history.stage_hours(window.start.to_pydatetime(), window.end.to_pydatetime())
    assert summary["merged"] == len(expected["coding"])
    for stage, values in expected.items():
        assert summary["stages"][stage]["count"] == len(values)
        assert summary["stages"][stage]["median_hours"] == pytest.approx(statistics.median(values))
    medians = {stage: statistics.median(values) for stage, values in expected.items()}
    assert summary["bottleneck"] == max(medians, key=medians.__getitem__)
    assert summary["bottleneck_share"] == pytest.approx(
        medians[summary["bottleneck"]] / sum(medians.values())
    )


def test_pr_flow_by_month(history, window, items):
    stages = flow.pr_stages(items, history.pr_frame(), window)
    monthly = flow.pr_flow_by_month(stages)

    assert monthly["merged"].sum() == len(stages)
    may = [
        pr
        for pr in history.pull_requests
        if pr["number"] in set(stages["number"]) and pr["merged_at"].strftime("%Y-%m") == "2026-05"
    ]
    reviewed = [pr for pr in may if pr["first_review_at"] is not None]
    assert monthly.loc["2026-05", "merged"] == len(may)
    assert monthly.loc["2026-05", "reviewed_share"] == pytest.approx(len(reviewed) / len(may))


# --- statistical ground truth -----------------------------------------------------------

# A year at high volume; the window is the second half, so long items have had time to
# start before it.
LARGE = Scenario(days=364, issues_per_day=20, prs_per_day=10)


@pytest.fixture(scope="module")
def large():
    history = generate(seed=11, scenario=LARGE)
    return history, history.frame(), Window.ending(history.as_of, 26)


def test_cycle_time_percentiles_recover_the_true_distribution(large):
    _, items, window = large
    cycle = flow.time_summary(flow.durations(items, window), "cycle_days")["issue"]["all"]

    assert cycle["count"] > 2000
    for p in (50, 85, 95):
        assert cycle[f"p{p}"] == pytest.approx(LARGE.cycle_quantile(p), rel=0.1)


def test_throughput_matches_the_arrival_rates(large):
    _, items, window = large
    weekly = flow.weekly_throughput(items, window)

    assert weekly["issue"].mean() == pytest.approx(LARGE.issues_delivered_per_day * 7, rel=0.05)
    assert weekly["pr"].mean() == pytest.approx(LARGE.prs_merged_per_day * 7, rel=0.05)


def test_wip_obeys_littles_law(large):
    _, items, window = large
    wip = flow.daily_wip(items, window)

    expected = LARGE.issues_started_per_day * LARGE.cycle_mean_days  # L = λW
    assert wip["issue"].mean() == pytest.approx(expected, rel=0.05)


def test_pr_flow_recovers_stage_medians_and_the_bottleneck(large):
    history, items, window = large
    summary = flow.pr_flow(flow.pr_stages(items, history.pr_frame(), window))

    truth = {
        "coding": LARGE.coding_hours,
        "waiting_for_review": LARGE.review_wait_hours,
        "in_review": LARGE.review_hours,
        "waiting_to_merge": LARGE.merge_wait_hours,
    }
    for stage, median in truth.items():
        assert summary["stages"][stage]["median_hours"] == pytest.approx(median, rel=0.1)
    assert summary["bottleneck"] == "waiting_for_review"
    assert summary["bottleneck_share"] == pytest.approx(20 / 32, abs=0.03)
    # Unreviewed PRs skip the review stages but still count as merged.
    reviewed = summary["stages"]["waiting_for_review"]["count"] / summary["merged"]
    assert reviewed == pytest.approx(1 - LARGE.unreviewed_share, abs=0.02)


# --- edge cases -------------------------------------------------------------------------

FOUR_WEEKS = Window.ending(at(28, 12), 4)  # Monday at noon, after four whole weeks


def test_no_items_at_all():
    items = frame()

    durations = flow.durations(items, FOUR_WEEKS)
    assert durations.empty
    summary = flow.time_summary(durations, "cycle_days")
    assert summary["issue"]["all"] == {
        "count": 0,
        "p50": None,
        "p85": None,
        "p95": None,
        "histogram": dict.fromkeys(HISTOGRAM_BINS, 0),
    }
    weekly = flow.weekly_throughput(items, FOUR_WEEKS)
    assert weekly.shape == (4, 3) and weekly.to_numpy().sum() == 0
    assert flow.throughput_summary(weekly["total"]) == {
        "mean": 0.0,
        "min": 0,
        "max": 0,
        "zero_weeks": 4,
    }
    assert flow.daily_wip(items, FOUR_WEEKS).to_numpy().sum() == 0
    assert flow.cumulative_flow(items, FOUR_WEEKS).to_numpy().sum() == 0
    assert flow.aging_wip(items, FOUR_WEEKS.as_of, {}).empty
    assert flow.pr_flow(flow.pr_stages(items, no_prs(), FOUR_WEEKS)) == {
        "merged": 0,
        "stages": {
            stage: {"count": 0, "median_hours": None}
            for stage in ("coding", "waiting_for_review", "in_review", "waiting_to_merge")
        },
        "bottleneck": None,
        "bottleneck_share": None,
    }


def test_all_items_in_progress():
    items = frame(
        work_item(1, created=at(0), started=at(1)),
        work_item(2, created=at(0), started=at(10)),
        work_item(3, created=at(2), started=at(3), type_="pr"),
    )

    summary = flow.time_summary(flow.durations(items, FOUR_WEEKS), "cycle_days")
    assert summary["issue"]["all"]["p85"] is None
    assert flow.weekly_throughput(items, FOUR_WEEKS)["total"].sum() == 0
    wip = flow.daily_wip(items, FOUR_WEEKS)
    # Started on days 1 and 3 (the PR) count from the end of those days; day 10 later.
    assert list(wip["total"].iloc[:4]) == [0, 1, 1, 2]
    assert (wip["total"].iloc[10], wip["total"].iloc[-1]) == (3, 3)
    aging = flow.aging_wip(items, FOUR_WEEKS.as_of, {"issue": summary["issue"]["all"]})
    assert list(aging["id"]) == ["issue#1", "pr#3", "issue#2"]
    assert aging["age_days"].iloc[0] == pytest.approx(27.5)
    assert not aging["over_p85"].any()  # nothing to compare against yet


def test_items_finished_on_the_day_they_started():
    items = frame(
        work_item(1, created=at(2, 9), started=at(2, 10), done=at(2, 15)),
        work_item(2, created=at(2, 9), done=at(2, 11)),  # closed without starting
    )

    durations = flow.durations(items, FOUR_WEEKS).set_index("id")
    assert durations.loc["issue#1", "cycle_days"] == pytest.approx(5 / 24)
    assert durations.loc["issue#1", "lead_days"] == pytest.approx(6 / 24)
    assert pd.isna(durations.loc["issue#2", "cycle_days"])
    cycle = flow.time_summary(durations.reset_index(), "cycle_days")["issue"]["all"]
    assert cycle["histogram"]["0-1d"] == 1
    assert flow.daily_wip(items, FOUR_WEEKS)["total"].sum() == 0  # never open at midnight
    cfd = flow.cumulative_flow(items, FOUR_WEEKS)
    assert list(cfd.loc[pd.Timestamp(at(2)), ["todo", "in_progress", "done"]]) == [0, 0, 2]
    assert flow.weekly_throughput(items, FOUR_WEEKS)["issue"].tolist() == [2, 0, 0, 0]


def test_weeks_with_zero_throughput_are_kept():
    items = frame(
        work_item(1, created=at(0), started=at(1), done=at(3)),
        work_item(2, created=at(0), started=at(1), done=at(17)),
        work_item(3, created=at(0), started=at(1), done=at(18)),
    )

    weekly = flow.weekly_throughput(items, FOUR_WEEKS)
    assert weekly["issue"].tolist() == [1, 0, 2, 0]
    assert [week.weekday() for week in weekly.index] == [0, 0, 0, 0]
    assert flow.throughput_summary(weekly["issue"]) == {
        "mean": 0.75,
        "min": 0,
        "max": 2,
        "zero_weeks": 2,
    }


def test_window_boundaries():
    window = FOUR_WEEKS  # [Mon 0, Mon 28), as_of Mon 28 at noon
    items = frame(
        work_item(1, created=at(-3), started=at(-2), done=at(0)),  # first instant: in
        work_item(2, created=at(-3), started=at(-2), done=at(-1, 23)),  # before: out
        work_item(3, created=at(20), started=at(21), done=at(28)),  # partial week: out
    )

    assert set(flow.durations(items, window)["id"]) == {"issue#1"}
    assert flow.weekly_throughput(items, window)["issue"].tolist() == [1, 0, 0, 0]
    # Cumulative flow runs to as_of, so it sees the partial week; "done" starts at 0.
    cfd = flow.cumulative_flow(items, window)
    assert cfd["done"].iloc[0] == 1 and cfd["done"].iloc[-1] == 2
    assert cfd.index[-1] == pd.Timestamp(at(28))


def test_excluded_items_never_count():
    items = frame(
        work_item(1, created=at(0), started=at(1), done=at(2), excluded=True),
        work_item(2, created=at(0), started=at(1), excluded=True, type_="pr"),
        work_item(3, created=at(0), excluded=True),
    )

    assert flow.durations(items, FOUR_WEEKS).empty
    assert flow.cumulative_flow(items, FOUR_WEEKS).to_numpy().sum() == 0
    assert flow.aging_wip(items, FOUR_WEEKS.as_of, {}).empty


def test_an_abandoned_pr_leaves_wip_without_being_delivered():
    items = frame(
        work_item(1, created=at(1), started=at(0, 12), done=at(5), outcome="abandoned",
                  type_="pr"),
    )  # fmt: skip

    assert flow.durations(items, FOUR_WEEKS).empty
    assert flow.weekly_throughput(items, FOUR_WEEKS)["pr"].sum() == 0
    cfd = flow.cumulative_flow(items, FOUR_WEEKS, "pr")
    # The first commit came before the PR opened: in progress from day 0, never to do.
    assert cfd["in_progress"].iloc[:6].tolist() == [1, 1, 1, 1, 1, 0]
    assert cfd["todo"].sum() == 0 and cfd["done"].sum() == 0


def test_a_never_started_issue_waits_in_to_do_then_leaves():
    items = frame(work_item(1, created=at(1, 6), done=at(4, 6)))

    cfd = flow.cumulative_flow(items, FOUR_WEEKS)
    assert cfd["todo"].iloc[:5].tolist() == [0, 1, 1, 1, 0]
    assert cfd["in_progress"].sum() == 0
    assert cfd["done"].iloc[4] == 1


def test_a_start_dated_after_the_finish_counts_as_the_finish():
    # A PR whose only commit was re-authored after the merge.
    items = frame(
        work_item(1, created=at(1), started=at(6), done=at(3), type_="pr"),
    )

    assert flow.durations(items, FOUR_WEEKS)["cycle_days"].tolist() == [0.0]
    cfd = flow.cumulative_flow(items, FOUR_WEEKS, "pr")
    assert cfd["in_progress"].min() == 0 and cfd["in_progress"].sum() == 0


def test_label_groups_put_an_item_in_every_kind_it_has():
    items = frame(
        work_item(1, created=at(0), started=at(1), done=at(2), kinds=["bug", "incident"]),
        work_item(2, created=at(0), started=at(1), done=at(4), kinds=["bug"]),
        work_item(3, created=at(0), started=at(1), done=at(8)),
    )

    summary = flow.time_summary(flow.durations(items, FOUR_WEEKS), "cycle_days")["issue"]
    counts = {group: summary[group]["count"] for group in summary}
    assert counts == {"all": 3, "bug": 2, "incident": 1, "hotfix": 0, "other": 1}
    assert summary["bug"]["p50"] == 1.0  # nearest rank of [1, 3]


def test_percentiles_are_nearest_rank():
    values = [float(v) for v in range(10, 0, -1)]

    assert flow.percentiles(values) == {"p50": 5.0, "p85": 9.0, "p95": 10.0}
    assert flow.percentiles([7.0]) == {"p50": 7.0, "p85": 7.0, "p95": 7.0}
    assert flow.percentiles([float("nan"), 2.0]) == {"p50": 2.0, "p85": 2.0, "p95": 2.0}


def test_reviews_after_the_merge_dont_count():
    items = frame(work_item(1, created=at(1), started=at(0), done=at(2), type_="pr"))
    prs = flow.pull_request_frame(
        [
            {
                "number": 1,
                "first_commit_at": at(0),
                "created_at": at(1),
                "first_review_at": at(3),  # a post-merge comment review
                "first_approval_at": at(3),
                "merged_at": at(2),
            }
        ]
    )

    stages = flow.pr_stages(items, prs, FOUR_WEEKS).iloc[0]
    assert stages["coding"] == 24
    assert pd.isna(stages["waiting_for_review"]) and pd.isna(stages["waiting_to_merge"])
    summary = flow.pr_flow(flow.pr_stages(items, prs, FOUR_WEEKS))
    assert summary["bottleneck"] == "coding" and summary["bottleneck_share"] == 1.0


def test_pr_stages_need_a_merged_counted_pr_in_the_window():
    items = frame(
        work_item(1, created=at(1), started=at(0), done=at(2), type_="pr"),
        work_item(2, created=at(1), started=at(0), done=at(2), type_="pr", excluded=True),
        work_item(3, created=at(1), started=at(0), done=at(2), type_="pr",
                  outcome="abandoned"),
        work_item(4, created=at(1), started=at(0), type_="pr"),
    )  # fmt: skip
    prs = flow.pull_request_frame(
        [{"number": n, "created_at": at(1), "first_commit_at": at(0)} for n in (1, 2, 3, 4)]
    )

    assert flow.pr_stages(items, prs, FOUR_WEEKS)["number"].tolist() == [1]
