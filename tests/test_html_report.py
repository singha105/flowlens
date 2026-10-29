"""The HTML report: one self-contained page, safe against what issue titles contain."""

import csv
import json
import re
from dataclasses import replace
from datetime import UTC, datetime

from flowlens.html_report import build_report, render_page, write_site
from repos import synthetic_repo

GENERATED = datetime(2026, 7, 6, 9, tzinfo=UTC)


def figures(page: str) -> dict:
    (data,) = re.findall(r"const FIGURES = (\{.*?\});\n", page)
    return json.loads(data)


def test_the_page_needs_nothing_but_itself():
    report = build_report(synthetic_repo(), trials=500)

    page = render_page(report, GENERATED)

    assert page.startswith("<!doctype html>")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import", page)
    assert "plotly.js v" in page  # inlined, not fetched
    assert [s for s in ("flow", "forecast", "dora", "pr-flow") if f'<section id="{s}">' in page]
    assert len(figures(page)) == len(re.findall(r'<div class="plot" id="', page)) >= 10


def test_the_page_opens_with_the_three_bullet_summary():
    report = build_report(synthetic_repo(), trials=500)

    page = render_page(report, GENERATED)

    assert len(report.bullets) == 3
    summary = page[page.index('<div class="summary">') : page.index("<nav")]
    assert summary.count("<li>") == 3
    assert "<strong>DORA:</strong>" in summary
    assert "Generated Jul 6, 2026, 09:00 UTC." in page


def test_a_hostile_title_stays_text():
    data = synthetic_repo()
    hostile = "</script><script>alert(1)</script><b>"
    items = data.items.copy()
    items.loc[items["type"] == "pr", "title"] = hostile
    report = build_report(replace(data, items=items), trials=500)

    page = render_page(report, GENERATED)

    assert page.count("</script>") == 2  # plotly.js and the page's own script
    assert hostile not in page
    assert any(hostile in json.dumps(f) for f in figures(page).values())  # kept, as data


def test_the_site_keeps_the_data_behind_the_page(tmp_path):
    report = build_report(synthetic_repo(), trials=500)

    page = write_site(report, tmp_path / "site")

    data = tmp_path / "site" / "data"
    assert page == tmp_path / "site" / "index.html"
    assert sorted(p.name for p in data.iterdir()) == [
        "backtest.json",
        "dora.json",
        "dora_failures.csv",
        "flow.json",
        "forecast_how_many.json",
        "forecast_when.json",
    ]
    with (data / "dora_failures.csv").open() as handle:
        assert [row["deploy"] for row in csv.DictReader(handle)] == ["v1", "v2"]
    for name in ("flow.json", "dora.json", "dora_failures.csv"):
        assert f'href="data/{name}"' in page.read_text()


def test_a_repo_too_young_to_forecast_still_gets_a_page():
    young = synthetic_repo(days=21)  # three weeks: less than the 12 a forecast resamples

    report = build_report(young, trials=500)
    page = render_page(report, GENERATED)

    assert report.when is None and report.how_many is None
    assert report.backtest["scores"] == []
    assert report.forecast_note.startswith("No forecast:")
    assert report.forecast_note in page
    assert "too little history to backtest" in page
    assert "Not enough history to backtest" in page
