"""FlowLens dashboard: ``uv run streamlit run app/dashboard.py``.

Reads the databases ``flowlens ingest`` writes (./data, or $FLOWLENS_DATA_DIR) as the last
flowlens command built their model, and draws the charts ``flowlens report`` publishes,
for any repo and date range. With no local data it downloads the published snapshot of
the demo repo, which is how it runs on Streamlit Community Cloud.
"""

import os
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st

from flowlens import charts
from flowlens.charts import NOUNS, Chart, date_text
from flowlens.config import load_config
from flowlens.metrics.dora_report import failure_rows
from flowlens.metrics.flow import DAY, WEEK, Window
from flowlens.metrics.forecast import ForecastError
from flowlens.metrics.forecast_report import backtest_report, forecast_report
from flowlens.metrics.report import STAGE_NAMES
from flowlens.snapshot import RepoData, load_repo, repos_in
from flowlens.storage import Store, db_path_for
from flowlens.summary import (
    Tile,
    dora_tiles,
    executive_summary,
    flow_tiles,
    forecast_tiles,
    pr_tiles,
)

DATA_DIR = Path(os.environ.get("FLOWLENS_DATA_DIR", "data"))
SNAPSHOT_URL = os.environ.get(
    "FLOWLENS_SNAPSHOT_URL",
    "https://singha105.github.io/flowlens/data/streamlit__streamlit.duckdb",
)
DEMO_REPO = "streamlit/streamlit"
TABS = ["Flow", "Forecast", "DORA", "PR flow"]
BAND_COLORS = {"elite": "green", "high": "green", "medium": "orange", "low": "red"}
PLOT_CONFIG = {"displayModeBar": False, "responsive": True}
LINKS = {"url": st.column_config.LinkColumn("link", display_text="open")}

st.set_page_config(page_title="FlowLens", page_icon=":material/insights:", layout="wide")


# --- data -------------------------------------------------------------------------------


def db_path(repo: str) -> Path:
    owner, name = repo.split("/")
    return db_path_for(DATA_DIR, owner, name)


@st.cache_resource(show_spinner="Reading the repo's history…", max_entries=4)
def load(repo: str, version: str) -> RepoData:
    with Store(db_path(repo), read_only=True) as store:
        return load_repo(store, load_config(None, repo), repo)


@st.cache_data(max_entries=32)
def views(repo: str, version: str, window: Window) -> tuple[dict, pd.DataFrame, dict]:
    data = load(repo, version)
    return data.flow(window), data.finished(window), data.dora(window)


@st.cache_data(max_entries=32)
def forecast(repo: str, version: str, unit: str, history: int, **question) -> dict:
    throughput = load(repo, version).throughput(unit)
    return forecast_report(throughput, history_weeks=history, **question)


@st.cache_data(max_entries=8)
def backtest(repo: str, version: str, unit: str) -> dict | None:
    try:
        return backtest_report(load(repo, version).throughput(unit))
    except ForecastError:
        return None


def fetch_snapshot() -> None:
    target = db_path(DEMO_REPO)
    target.parent.mkdir(parents=True, exist_ok=True)
    with st.spinner(f"Downloading the published {DEMO_REPO} snapshot…"):
        partial = target.with_suffix(".part")
        urllib.request.urlretrieve(SNAPSHOT_URL, partial)  # a fixed https URL
        partial.rename(target)


# --- pieces -----------------------------------------------------------------------------


def tiles(items: list[Tile]) -> None:
    for column, tile in zip(st.columns(len(items)), items, strict=True):
        with column.container(border=True):
            help_text = f"Band limits: {tile.source}" if tile.source else None
            st.metric(tile.label, tile.value, help=help_text)
            if tile.band:
                st.markdown(f":{BAND_COLORS[tile.band]}-badge[{tile.band.capitalize()} band]")
            st.caption(tile.note)


def show(chart: Chart) -> None:
    with st.container(border=True):
        st.markdown(f"**{chart.title}**")
        st.plotly_chart(chart.figure, theme=None, config=PLOT_CONFIG, key=chart.key)
        st.caption(chart.caption)
        with st.expander(f"Table: {len(chart.table):,} rows"):
            st.dataframe(chart.table, hide_index=True, column_config=LINKS)


def noun(type_: str) -> str:
    plural = NOUNS[type_][1]
    return plural[:1].upper() + plural[1:]


# --- page -------------------------------------------------------------------------------

if not repos_in(DATA_DIR):
    try:
        fetch_snapshot()
    except OSError as exc:
        st.error(
            f"No data in {DATA_DIR}/ and the published snapshot couldn't be downloaded ({exc}). "
            "Run `uv run flowlens ingest --repo owner/name` first, then reload."
        )
        st.stop()

repos = repos_in(DATA_DIR)
st.markdown("## FlowLens")
filters = st.columns([2, 3, 4], vertical_alignment="bottom")
repo = filters[0].selectbox(
    "Repository", repos, index=repos.index(DEMO_REPO) if DEMO_REPO in repos else 0
)
# The cache key: which file, and which version of it.
version = f"{db_path(repo).resolve()}@{db_path(repo).stat().st_mtime_ns}"
data = load(repo, version)
latest = data.window(26)
first_day = data.first_week.date()
last_day = (data.latest_end - DAY).date()
picked = filters[1].date_input(
    "Date range",
    value=(max(latest.start.date(), first_day), last_day),
    min_value=first_day,
    max_value=last_day,
    help="Rounded out to whole weeks, Monday to Sunday (UTC). WIP and aging are as of the "
    "range's end.",
)
if not isinstance(picked, tuple) or len(picked) != 2:
    st.stop()  # the second date isn't picked yet
window = data.window_between(*picked)
as_of = data.as_of
filters[2].markdown(
    f"{window.weeks} whole weeks, {date_text(window.start)} to "
    f"{date_text(window.end - DAY)}  \n"
    f":gray[Data as of {date_text(as_of)}, {as_of:%H:%M} UTC, from the history since "
    f"{date_text(data.first_week)}]"
)

flow, finished, dora = views(repo, version, window)
summary_backtest = backtest(repo, version, "issue")
with st.container(border=True):
    st.markdown(
        "\n".join(
            f"- **{bullet.lead}:** {bullet.text}"
            for bullet in executive_summary(flow, dora, summary_backtest)
        )
    )

flow_tab, forecast_tab, dora_tab, pr_tab = st.tabs(TABS, key="tab", bind="query-params")

with flow_tab:
    type_ = st.segmented_control(
        "Items", ["pr", "issue"], default="pr", format_func=noun, required=True, key="flow_type"
    )
    tiles(flow_tiles(flow, type_))
    show(charts.cycle_time_scatter(finished, flow, type_))
    left, right = st.columns(2)
    with left:
        show(charts.throughput_chart(flow))
    with right:
        show(charts.cumulative_flow_chart(flow, type_))
    show(charts.aging_wip_chart(flow))
    for note in flow["notes"]:
        st.caption(f"Note: {note}")

with forecast_tab:
    settings = st.columns([2, 2, 5], vertical_alignment="bottom")
    unit = settings[0].segmented_control(
        "Forecast",
        ["issue", "pr"],
        default="issue",
        format_func=noun,
        required=True,
        key="unit",
    )
    history = settings[1].number_input("Weeks of history", min_value=1, max_value=52, value=12)
    start = data.throughput(unit).next_start
    settings[2].caption(
        f"Forecasts start {date_text(start)} and resample the last {history} complete weeks "
        "of throughput before it, whatever the date range above."
    )
    plural = NOUNS[unit][1]
    left, right = st.columns(2)
    with left:
        items = st.number_input(
            f"When will this many {plural} be done?", min_value=1, value=40, step=5
        )
        try:
            when = forecast(repo, version, unit, history, items=items)
        except ForecastError as exc:
            st.warning(str(exc))
        else:
            tiles(forecast_tiles(when, None))
            show(charts.when_chart(when))
    with right:
        by = st.date_input(
            f"How many {plural} by this date?",
            value=(start + 8 * WEEK - DAY).date(),
            min_value=start.date(),
        )
        try:
            how_many = forecast(repo, version, unit, history, by=pd.Timestamp(by, tz="UTC"))
        except (ForecastError, ValueError) as exc:
            st.warning(str(exc))
        else:
            tiles(forecast_tiles(None, how_many))
            show(charts.how_many_chart(how_many))
    scores = backtest(repo, version, unit)
    if scores is None:
        st.info("Not enough history to backtest: ingest further back with --since.")
    else:
        horizon = st.segmented_control(
            "Backtest horizon",
            [4, 8],
            default=4,
            format_func=lambda h: f"{h} weeks",
            required=True,
            key="horizon",
        )
        show(charts.calibration_chart(scores, horizon, history))

with dora_tab:
    tiles(dora_tiles(dora))
    left, right = st.columns(2)
    with left:
        show(charts.deploys_chart(dora))
        show(charts.failure_rate_trend(dora))
    with right:
        show(charts.lead_time_trend(dora))
        show(charts.recovery_chart(dora))
    st.markdown("**Failed deployments**")
    failed = charts.failed_deploys_table(failure_rows(dora))
    if failed.empty:
        st.caption("None in this window.")
    else:
        st.caption("Every failure signal that counted against a deploy, and what fixed it.")
        st.dataframe(
            failed,
            hide_index=True,
            column_config={"URL": st.column_config.LinkColumn("Link", display_text="open")},
        )
    for key, band in dora["bands"].items():
        st.caption(f"{key.replace('_', ' ').capitalize()} band limits: {band['source']}")

with pr_tab:
    tiles(pr_tiles(flow))
    show(charts.pr_flow_chart(flow))
    stages = flow["pr_flow"]["stages"]
    st.dataframe(
        pd.DataFrame(
            {
                "Stage": [STAGE_NAMES[s][:1].upper() + STAGE_NAMES[s][1:] for s in stages],
                "Median hours": [round(v["median_hours"] or 0, 1) for v in stages.values()],
                "PRs measured": [v["count"] for v in stages.values()],
            }
        ),
        hide_index=True,
    )
