"""Monte Carlo forecasts and their backtest.

Ground truth here is either exact (constant throughput leaves nothing to chance; tiny
histories can be enumerated) or a property every forecast must have: a fixed seed repeats,
percentiles are ordered, and no forecast can see a week after its start.
"""

import itertools
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fakes import fill_with_streamlit
from flowlens.config import load_config
from flowlens.metrics.forecast import (
    CONFIDENCES,
    ForecastError,
    backtest,
    backtest_origins,
    history_before,
    how_many,
    score_backtest,
    when,
)
from flowlens.metrics.forecast_report import (
    Throughput,
    backtest_report,
    forecast_report,
    format_backtest,
    format_forecast,
    load_throughput,
)
from flowlens.model.build import build_model
from flowlens.storage import Store

FIRST_WEEK = pd.Timestamp("2025-11-03", tz="UTC")  # a Monday


def series(counts) -> pd.Series:
    """Weekly throughput starting FIRST_WEEK, shaped like flow.weekly_throughput's."""
    index = pd.date_range(FIRST_WEEK, periods=len(counts), freq="7D")
    return pd.Series(list(counts), index=index, name="issue")


def monday_after(weekly: pd.Series) -> pd.Timestamp:
    return weekly.index[-1] + pd.Timedelta(weeks=1)


CONSTANT = series([5] * 20)
START = monday_after(CONSTANT)  # 2026-03-23


# --- history ----------------------------------------------------------------------------


def test_history_is_the_last_complete_weeks_before_start():
    weekly = series(range(10))  # 0, 1, ..., 9

    assert history_before(weekly, monday_after(weekly), 3).tolist() == [7, 8, 9]
    # Mid-week, the week in progress isn't complete yet.
    midweek = weekly.index[-1] + pd.Timedelta(days=3)
    assert history_before(weekly, midweek, 3).tolist() == [6, 7, 8]
    assert history_before(weekly, date(2025, 11, 17), 2).tolist() == [0, 1]


def test_too_little_history_is_an_error():
    with pytest.raises(ForecastError, match="12 weeks of history needed before 2025-12-01"):
        history_before(series([1, 2, 3, 4]), date(2025, 12, 1), 12)
    with pytest.raises(ValueError, match="at least 1"):
        history_before(CONSTANT, START, 0)


# --- exact results with constant throughput ---------------------------------------------


def test_constant_throughput_makes_when_exact():
    forecast = when(CONSTANT, 40, START, seed=1)

    assert set(forecast.weeks) == {8}
    assert forecast.dates == dict.fromkeys(CONFIDENCES, START + pd.Timedelta(weeks=8))
    assert set(when(CONSTANT, 41, START).weeks) == {9}
    assert set(when(CONSTANT, 0, START).weeks) == {0}


def test_constant_throughput_makes_how_many_exact():
    eight_weeks = START + pd.Timedelta(weeks=8, days=-1)  # a Sunday

    assert set(how_many(CONSTANT, eight_weeks, START).items) == {40}
    # A Thursday only gets the whole weeks before it.
    thursday = START + pd.Timedelta(weeks=8, days=3)
    forecast = how_many(CONSTANT, thursday, START)
    assert (forecast.weeks, forecast.through) == (8, eight_weeks)
    assert forecast.counts == dict.fromkeys(CONFIDENCES, 40)
    assert set(how_many(CONSTANT, START, START).items) == {0}  # no whole week


def test_a_throughput_drop_scales_every_week():
    assert set(when(CONSTANT, 40, START, throughput_factor=0.8).weeks) == {10}
    eight_weeks = START + pd.Timedelta(weeks=8, days=-1)
    assert set(how_many(CONSTANT, eight_weeks, START, throughput_factor=0.8).items) == {32}


def test_by_before_start_is_an_error():
    with pytest.raises(ValueError, match="before start"):
        how_many(CONSTANT, START - pd.Timedelta(days=1), START)


# --- seeds, ordering, and percentile meaning --------------------------------------------

POISSON = series(np.random.default_rng(42).poisson(12, size=40))


def test_a_fixed_seed_repeats_exactly():
    start = monday_after(POISSON)
    by = start + pd.Timedelta(weeks=6)

    first, again = when(POISSON, 60, start, seed=7), when(POISSON, 60, start, seed=7)
    assert np.array_equal(first.weeks, again.weeks)
    assert np.array_equal(
        how_many(POISSON, by, start, seed=7).items, how_many(POISSON, by, start, seed=7).items
    )
    assert not np.array_equal(
        when(POISSON, 60, start, seed=7).weeks, when(POISSON, 60, start, seed=8).weeks
    )


@pytest.mark.parametrize("seed", range(5))
def test_percentiles_are_ordered(seed):
    rng = np.random.default_rng(seed)
    weekly = series(rng.poisson(rng.uniform(1, 30), size=30) * rng.integers(0, 2, size=30))
    start = monday_after(weekly)

    dates = when(weekly, int(rng.integers(1, 200)), start, seed=seed).dates
    assert dates[50] <= dates[85] <= dates[95]
    counts = how_many(weekly, start + pd.Timedelta(weeks=10), start, seed=seed).counts
    assert counts[95] <= counts[85] <= counts[50]


def test_confidence_means_what_it_says():
    start = monday_after(POISSON)
    forecast = when(POISSON, 100, start, trials=2_000, seed=3)
    for c in CONFIDENCES:
        weeks = forecast.weeks_at(c)
        assert np.mean(forecast.weeks <= weeks) >= c / 100  # c% finished by then...
        assert np.mean(forecast.weeks <= weeks - 1) < c / 100  # ...and not a week earlier

    counts = how_many(POISSON, start + pd.Timedelta(weeks=8), start, trials=2_000, seed=3)
    for c in CONFIDENCES:
        n = counts.at_least(c)
        assert np.mean(counts.items >= n) >= c / 100  # c% reached at least n...
        assert np.mean(counts.items >= n + 1) < c / 100  # ...and no higher n holds


# --- the sampling itself ----------------------------------------------------------------


def test_how_many_matches_the_enumerated_distribution():
    weekly = series([1, 2, 3, 0])
    start = monday_after(weekly)
    forecast = how_many(weekly, start + pd.Timedelta(weeks=3, days=-1), start, history_weeks=4,
                        trials=40_000, seed=0)  # fmt: skip

    outcomes = [sum(draw) for draw in itertools.product([1, 2, 3, 0], repeat=3)]
    expected = {n: k / len(outcomes) for n, k in Counter(outcomes).items()}
    distribution = forecast.distribution()
    shares = dict(zip(distribution["items"], distribution["share"], strict=True))
    assert shares.keys() == expected.keys()
    for n, share in expected.items():
        assert shares[n] == pytest.approx(share, abs=0.01)


def test_when_matches_the_enumerated_distribution():
    history = [0, 1, 4]
    weekly = series(history)
    start = monday_after(weekly)
    forecast = when(weekly, 4, start, history_weeks=3, trials=40_000, seed=0)

    # P(done within w weeks) = P(the first w draws sum to at least 4).
    for w in range(1, 6):
        draws = list(itertools.product(history, repeat=w))
        expected = sum(sum(d) >= 4 for d in draws) / len(draws)
        assert np.mean(forecast.weeks <= w) == pytest.approx(expected, abs=0.01)


def test_zero_weeks_are_resampled():
    weekly = series([0, 10])
    start = monday_after(weekly)
    forecast = when(weekly, 10, start, history_weeks=2, seed=0)

    # Geometric: half the trials finish in week 1, a quarter in week 2, ...
    assert np.mean(forecast.weeks == 1) == pytest.approx(0.5, abs=0.02)
    assert np.mean(forecast.weeks == 2) == pytest.approx(0.25, abs=0.02)
    assert forecast.weeks.max() > 5


def test_slow_trials_run_on_until_they_finish():
    weekly = series([0] * 19 + [100])  # one week in 20 delivers anything
    forecast = when(weekly, 1, monday_after(weekly), history_weeks=20, seed=0)

    assert forecast.weeks.mean() == pytest.approx(20, rel=0.05)
    assert forecast.weeks.max() > 100


def test_forecasts_that_never_finish_are_errors():
    zeros = series([0] * 12)
    with pytest.raises(ForecastError, match="nothing was delivered"):
        when(zeros, 5, monday_after(zeros))
    assert set(when(zeros, 0, monday_after(zeros)).weeks) == {0}

    rare = series([0] * 999 + [1])
    with pytest.raises(ForecastError, match="more than 520 weeks"):
        when(rare, 1, monday_after(rare), history_weeks=1000)


def test_ten_thousand_trials_take_well_under_a_second():
    start = monday_after(POISSON)

    began = time.perf_counter()
    when(POISSON, 1_000, start, trials=10_000, seed=0)
    how_many(POISSON, start + pd.Timedelta(weeks=52), start, trials=10_000, seed=0)
    assert time.perf_counter() - began < 1.0


# --- backtest ---------------------------------------------------------------------------

LONG = series(np.random.default_rng(7).poisson(15, size=100))


def test_backtest_origins_are_the_latest_scorable_weeks():
    origins = backtest_origins(LONG, horizon=8, history_weeks=26, origins=52)

    assert len(origins) == 52
    assert origins[-1] == LONG.index[100 - 8]  # its 8 weeks end with the data
    assert origins[0] == LONG.index[100 - 8 - 51]
    assert len(backtest_origins(LONG[:40], 8, 26, 52)) == 40 - 8 - 26 + 1  # what fits


def test_constant_throughput_backtests_perfectly():
    rows = backtest(series([5] * 60), trials=500)
    scores = score_backtest(rows)

    assert set(scores["origins"]) == {60 - 26 - 8 + 1, 60 - 26 - 4 + 1}
    assert scores[[f"held_{c}" for c in CONFIDENCES]].to_numpy().min() == 1.0
    assert scores["mae"].max() == 0 and scores["bias"].abs().max() == 0


def test_backtest_rows_score_against_what_happened():
    rows = backtest(LONG, trials=1_000)

    for row in rows.sample(20, random_state=0).itertuples():
        ahead = LONG[row.origin : row.origin + pd.Timedelta(weeks=row.horizon_weeks - 1)]
        assert row.actual == ahead.sum() and len(ahead) == row.horizon_weeks
        assert row.held_85 == (row.actual >= row.p85)
    scores = score_backtest(rows).loc[(4, 12)]
    four_twelve = rows[(rows.horizon_weeks == 4) & (rows.history_weeks == 12)]
    assert scores["held_85"] == four_twelve["held_85"].mean()
    assert scores["mae"] == pytest.approx((four_twelve.actual - four_twelve.p50).abs().mean())
    assert scores["bias"] == pytest.approx((four_twelve.actual - four_twelve.p50).mean())


def test_backtest_never_looks_ahead():
    cut = 70
    future = LONG.copy()
    future.iloc[cut:] *= 10  # the future changes completely

    before, after = backtest(LONG, trials=500), backtest(future, trials=500)
    forecasts = ["p50", "p85", "p95"]
    known = before["origin"] <= LONG.index[cut]
    assert known.any() and (~known).any()
    # A forecast made at or before the change is identical: it never saw the changed weeks.
    assert before[known][forecasts].equals(after[known][forecasts])
    # Later origins resample the changed weeks, so their forecasts move.
    assert not before[~known][forecasts].equals(after[~known][forecasts])


def test_resampling_short_histories_is_overconfident():
    """On stationary data a long history is close to calibrated; a short one isn't, because
    resampling a few weeks ignores how uncertain their average is."""
    weekly = series(np.random.default_rng(1).poisson(20, size=800))

    def scores(history: int) -> pd.Series:
        rows = backtest(weekly, horizons=(8,), histories=(history,), origins=500, trials=2_000)
        return score_backtest(rows).iloc[0]

    long_, short = scores(200), scores(6)
    assert long_["held_85"] == pytest.approx(0.85, abs=0.05)
    assert 0.5 <= long_["held_50"] <= 0.6  # "at least the median" holds a little over half
    assert short["held_85"] < long_["held_85"] - 0.05


# --- reports ----------------------------------------------------------------------------


def throughput(weekly: pd.Series, as_of: str = "2026-03-26T05:00:00Z") -> Throughput:
    return Throughput(repo="acme/widgets", unit="issue", weekly=weekly, as_of=pd.Timestamp(as_of))


@pytest.mark.parametrize(
    ("as_of", "start"),
    [
        ("2026-03-26T05:00:00Z", "2026-03-30"),  # Thursday: the next Monday
        ("2026-03-30T00:00:00Z", "2026-03-30"),  # Monday at midnight: today
        ("2026-03-30T00:00:01Z", "2026-04-06"),  # already underway: next week
    ],
)
def test_forecasts_start_on_the_next_monday(as_of, start):
    assert throughput(CONSTANT, as_of).next_start == pd.Timestamp(start, tz="UTC")


def test_when_report_and_its_text():
    report = forecast_report(throughput(CONSTANT), items=40, start=START)

    assert report["when"]["dates"] == dict.fromkeys(("50", "85", "95"), "2026-05-18T00:00:00Z")
    assert report["sensitivity"]["p85_weeks_later"] == 2
    assert report["history"]["weekly"] == [5] * 12
    assert format_forecast(report).splitlines() == [
        "Forecast: 40 issues from 2026-03-23 (12 weeks of history, 10,000 trials)",
        "  history: 2025-12-29 → 2026-03-22, 5.0 issues/week (min 5, max 5, 0 empty weeks)",
        "  50%: by 2026-05-18 | 85%: by 2026-05-18 | 95%: by 2026-05-18",
        "  If throughput drops 20%: 85% by 2026-06-01 (2 weeks later)",
    ]


def test_how_many_report_and_its_text():
    report = forecast_report(throughput(CONSTANT), by=datetime(2026, 5, 21, tzinfo=UTC))

    assert report["start"] == "2026-03-30T00:00:00Z"  # the Monday after as_of
    assert report["how_many"]["at_least"] == {"50": 35, "85": 35, "95": 35}
    assert format_forecast(report).splitlines() == [
        "Forecast: issues done from 2026-03-30 through 2026-05-17 "
        "(7 whole weeks; 12 weeks of history, 10,000 trials)",
        "  history: 2025-12-29 → 2026-03-22, 5.0 issues/week (min 5, max 5, 0 empty weeks)",
        "  50%: at least 35 | 85%: at least 35 | 95%: at least 35",
        "  If throughput drops 20%: 85%: at least 28 (7 fewer)",
    ]


def test_forecast_report_needs_exactly_one_question():
    with pytest.raises(ValueError, match="exactly one"):
        forecast_report(throughput(CONSTANT))
    with pytest.raises(ValueError, match="exactly one"):
        forecast_report(throughput(CONSTANT), items=1, by=datetime(2026, 5, 1, tzinfo=UTC))


def test_backtest_report_and_its_text():
    report = backtest_report(throughput(LONG, "2027-10-01T00:00:00Z"), trials=500)

    assert len(report["scores"]) == 6
    assert len(report["forecasts"]) == 2 * 3 * 52
    assert report["forecasts"][0].keys() == {"origin", "horizon_weeks", "history_weeks", "p50",
                                             "p85", "p95", "actual", "held_50", "held_85",
                                             "held_95"}  # fmt: skip
    text = format_backtest(report)
    assert text.startswith(
        "Backtest · acme/widgets · issues · 500 trials per forecast "
        "(100 weeks of measured history from 2025-11-03)\n\n4-week horizon: 52 weekly origins"
    )
    assert "  history     50% held  85% held  95% held   p50 MAE  p50 bias" in text
    first = report["scores"][0]
    assert f"   6 weeks  {first['held_50']:>10.0%}" in text


def test_backtest_report_without_enough_history():
    report = backtest_report(throughput(series([3] * 20)), trials=500)

    assert report["scores"] == [] and report["forecasts"] == []
    assert "Not enough history to backtest: it needs at least 30 measured weeks" in (
        format_backtest(report)
    )


def test_throughput_covers_only_fully_ingested_weeks(tmp_path):
    repo = "streamlit/streamlit"
    with Store(tmp_path / "streamlit.duckdb") as store:
        fill_with_streamlit(
            store,
            coverage_from=datetime(2025, 10, 29, tzinfo=UTC),  # a Wednesday
            finished_at=datetime(2026, 10, 29, 5, tzinfo=UTC),
        )
        build_model(store, load_config(Path(__file__).parent.parent / "flowlens.yaml", repo), repo)
        loaded = load_throughput(store, repo, "issue")
        (delivered,) = store.con.execute(
            "SELECT count(*) FROM work_items WHERE type = 'issue' AND NOT excluded "
            "AND outcome IN ('completed', 'closed_no_reason') "
            "AND done_at >= '2025-11-03' AND done_at < '2026-10-26'"
        ).fetchone()

    assert loaded.weekly.index[0] == pd.Timestamp("2025-11-03", tz="UTC")  # first whole week
    assert loaded.weekly.index[-1] == pd.Timestamp("2026-10-19", tz="UTC")  # last complete one
    assert len(loaded.weekly) == 51
    assert loaded.weekly.sum() == delivered == 2
    assert loaded.next_start == pd.Timestamp("2026-11-02", tz="UTC")
