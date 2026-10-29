"""What the dashboard and the report load: windows from date ranges, and the same numbers
as the command line."""

from datetime import date

import pandas as pd

from flowlens.config import load_config
from flowlens.metrics.dora_report import dora_report_for
from flowlens.metrics.forecast_report import load_throughput
from flowlens.metrics.report import flow_report_for
from flowlens.snapshot import load_repo, repos_in
from flowlens.storage import Store
from repos import ROOT, STREAMLIT, streamlit_db, synthetic_repo


def utc(day: str) -> pd.Timestamp:
    return pd.Timestamp(day, tz="UTC")


def test_a_date_range_rounds_out_to_whole_weeks():
    data = synthetic_repo()  # as of Monday 2026-07-06

    window = data.window_between(date(2026, 3, 4), date(2026, 4, 1))  # two Wednesdays

    assert (window.start, window.end) == (utc("2026-03-02"), utc("2026-04-06"))
    assert window.weeks == 5
    assert window.as_of == window.end  # WIP and aging as of the range's end


def test_a_range_up_to_the_last_ingest_keeps_its_time_and_stops_at_the_last_whole_week():
    data = synthetic_repo()

    to_the_end = data.window_between(date(2026, 5, 1), date(2026, 7, 5))
    beyond = data.window_between(date(2026, 5, 1), date(2026, 12, 31))

    assert to_the_end.end == beyond.end == data.latest_end == utc("2026-07-06")
    assert to_the_end.as_of == beyond.as_of == data.as_of


def test_a_single_day_gets_its_week():
    window = synthetic_repo().window_between(date(2026, 3, 4), date(2026, 3, 4))

    assert (window.start, window.end, window.weeks) == (utc("2026-03-02"), utc("2026-03-09"), 1)


def test_the_views_match_the_command_line_reports(tmp_path):
    streamlit_db(tmp_path)
    config = load_config(ROOT / "flowlens.yaml", STREAMLIT)
    with Store(tmp_path / "streamlit__streamlit.duckdb", read_only=True) as store:
        data = load_repo(store, config, STREAMLIT)
        flow = flow_report_for(store, STREAMLIT, weeks=26)
        dora = dora_report_for(store, config, STREAMLIT, weeks=26)
        throughput = load_throughput(store, STREAMLIT, "issue")

    window = data.window(26)
    assert data.flow(window) == flow
    assert data.dora(window) == dora
    pd.testing.assert_series_equal(data.throughput("issue").weekly, throughput.weekly)
    assert data.first_week == utc("2025-11-03")  # the first Monday the ingest fully covers


def test_finished_items_carry_their_finish_date_and_title():
    data = synthetic_repo()
    window = data.window(26)

    finished = data.finished(window)
    flow = data.flow(window)

    assert (
        len(finished)
        == flow["lead_time_days"]["issue"]["all"]["count"]
        + (flow["lead_time_days"]["pr"]["all"]["count"])
    )
    assert finished["done_at"].between(window.start, window.end, inclusive="left").all()
    assert finished["title"].notna().all()


def test_repos_are_found_by_database_name(tmp_path):
    for name in ("pallets__markupsafe.duckdb", "acme__widgets.duckdb", "scratch.duckdb", "x.txt"):
        (tmp_path / name).touch()

    assert repos_in(tmp_path) == ["acme/widgets", "pallets/markupsafe"]
    assert repos_in(tmp_path / "missing") == []
