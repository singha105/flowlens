"""The dashboard runs end to end on a recorded repo, and without data fetches the snapshot."""

from datetime import date

import pytest
from streamlit.testing.v1 import AppTest

from repos import ROOT, streamlit_db

APP = str(ROOT / "app" / "dashboard.py")


@pytest.fixture
def app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(ROOT)  # the repo's flowlens.yaml
    monkeypatch.setenv("FLOWLENS_DATA_DIR", str(tmp_path / "data"))
    return AppTest.from_file(APP, default_timeout=60)


def texts(app: AppTest) -> str:
    return "\n".join(m.value for m in [*app.markdown, *app.caption])


def test_every_tab_renders_the_recorded_repo(app, tmp_path):
    streamlit_db(tmp_path / "data")

    app.run()

    assert not app.exception
    assert not app.error
    assert [tab.label for tab in app.tabs] == ["Flow", "Forecast", "DORA", "PR flow"]
    assert app.selectbox[0].value == "streamlit/streamlit"
    shown = texts(app)
    for title in (
        "Cycle time: PRs",
        "Weekly throughput",
        "Aging work in progress",
        "Backtest: did 4-week issue forecasts hold?",
        "Where PR time goes, by month",
        "Deploys per week",
    ):
        assert title in shown
    assert "DORA:" in shown  # the summary bullets


def test_a_narrower_date_range_redraws_the_charts(app, tmp_path):
    streamlit_db(tmp_path / "data")
    app.run()
    dates = app.date_input[0]
    assert dates.label == "Date range"
    assert "26 whole weeks" in texts(app)

    dates.set_value((date(2026, 9, 2), date(2026, 10, 1))).run()  # Wednesday to Thursday

    assert not app.exception
    assert "5 whole weeks, Aug 31, 2026 to Oct 4, 2026" in texts(app)


def test_without_data_it_downloads_the_published_snapshot(app, tmp_path, monkeypatch):
    published = tmp_path / "published"
    snapshot = streamlit_db(published)
    monkeypatch.setenv("FLOWLENS_SNAPSHOT_URL", snapshot.as_uri())

    app.run()

    assert not app.exception and not app.error
    assert (tmp_path / "data" / snapshot.name).exists()
    assert not list((tmp_path / "data").glob("*.part"))


def test_an_unreachable_snapshot_explains_what_to_run(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLENS_SNAPSHOT_URL", (tmp_path / "missing.duckdb").as_uri())

    app.run()

    assert not app.exception
    (error,) = app.error
    assert "uv run flowlens ingest --repo owner/name" in error.value
