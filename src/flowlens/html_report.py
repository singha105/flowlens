"""``flowlens report``: one self-contained HTML page for people who won't run the CLI.

`build_report` computes every view of a repo (flow, forecast, backtest, DORA) with the
same functions as the other commands; `render_page` lays them out with an executive
summary, stat tiles, and the charts from flowlens.charts. The page inlines plotly.js and
needs nothing else, so it works from a file, GitHub Pages, or an email attachment.
`write_site` writes it as index.html beside the data it was built from.
"""

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.io as pio
from plotly.offline import get_plotlyjs

from flowlens import charts
from flowlens.charts import Chart, date_text
from flowlens.metrics.dora_report import failure_rows, write_failures
from flowlens.metrics.flow import DAY, WEEK
from flowlens.metrics.forecast import ForecastError
from flowlens.metrics.forecast_report import backtest_report, forecast_report
from flowlens.metrics.report import write_report
from flowlens.snapshot import RepoData
from flowlens.summary import (
    Bullet,
    Tile,
    dora_tiles,
    executive_summary,
    flow_tiles,
    forecast_tiles,
    pr_tiles,
)

TABLE_ROWS = 40  # longer tables are cut to their first rows, with a note
METRICS_URL = "https://github.com/singha105/flowlens/blob/main/docs/METRICS.md"
PROJECT_URL = "https://github.com/singha105/flowlens"
BAND_MARKS = {"elite": "good", "high": "good", "medium": "warning", "low": "critical"}


@dataclass(frozen=True)
class Report:
    repo: str
    as_of: pd.Timestamp
    window: dict[str, Any]
    bullets: list[Bullet]
    flow: dict[str, Any]
    dora: dict[str, Any]
    when: dict[str, Any] | None
    how_many: dict[str, Any] | None
    backtest: dict[str, Any] | None
    forecast_note: str | None
    sections: list["Section"]


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    intro: str
    tiles: list[Tile]
    charts: list[tuple[Chart, bool]]  # (chart, full width)
    after: str = ""  # extra HTML below the charts


def build_report(
    data: RepoData,
    *,
    weeks: int = 26,
    unit: str = "issue",
    items: int = 40,
    forecast_weeks: int = 8,
    history_weeks: int = 12,
    horizon: int = 4,
    trials: int = 10_000,
    seed: int = 0,
) -> Report:
    window = data.window(weeks)
    flow = data.flow(window)
    finished = data.finished(window)
    dora = data.dora(window)
    throughput = data.throughput(unit)
    options = {"history_weeks": history_weeks, "trials": trials, "seed": seed}
    when = how_many = backtest = None
    note = None
    try:
        when = forecast_report(throughput, items=items, **options)
        by = throughput.next_start + forecast_weeks * WEEK - DAY
        how_many = forecast_report(throughput, by=by.to_pydatetime(), **options)
    except ForecastError as exc:
        note = f"No forecast: {exc}."
    try:
        backtest = backtest_report(throughput, trials=trials, seed=seed)
    except ForecastError:
        backtest = None
    bullets = executive_summary(flow, dora, backtest, horizon=horizon, history_weeks=history_weeks)

    flow_charts = [
        (charts.cycle_time_scatter(finished, flow, "pr"), True),
        (charts.cycle_time_scatter(finished, flow, "issue"), True),
        (charts.throughput_chart(flow), False),
        (charts.cumulative_flow_chart(flow, "pr"), False),
        (charts.aging_wip_chart(flow), True),
    ]
    forecast_charts = []
    if when is not None and how_many is not None:
        forecast_charts += [
            (charts.when_chart(when), False),
            (charts.how_many_chart(how_many), False),
        ]
    if backtest is not None:
        forecast_charts.append((charts.calibration_chart(backtest, horizon, history_weeks), True))
    unit_name = charts.NOUNS[unit][1]
    sections = [
        Section(
            "flow",
            "Flow",
            "How long work takes and how much gets done. Cycle time runs from the first sign "
            "of work (a first commit, an assignment, a linked PR) to merged or closed.",
            flow_tiles(flow, "pr") + flow_tiles(flow, "issue"),
            flow_charts,
            _notes(flow["notes"]),
        ),
        Section(
            "forecast",
            "Forecast",
            f"Monte Carlo forecasts resample the last {history_weeks} weeks of {unit_name} "
            f"throughput {trials:,} times, starting {date_text(throughput.next_start)}. The "
            "backtest asks how often the same method held in the past.",
            forecast_tiles(when, how_many),
            forecast_charts,
            f'<p class="note">{html.escape(note)}</p>' if note else "",
        ),
        Section(
            "dora",
            "DORA",
            f"The four DORA keys, with deploys taken from {dora['deployment_source']}. Bands "
            "are approximate: DORA publishes clusters of teams, not cut-offs per metric.",
            dora_tiles(dora),
            [
                (charts.deploys_chart(dora), False),
                (charts.lead_time_trend(dora), False),
                (charts.failure_rate_trend(dora), False),
                (charts.recovery_chart(dora), False),
            ],
            _failed_deploys(dora) + _band_sources(dora),
        ),
        Section(
            "pr-flow",
            "PR flow",
            "Where a merged PR's time goes: coding before it opens, waiting for a first human "
            "review, review until approval, and waiting to merge.",
            pr_tiles(flow),
            [(charts.pr_flow_chart(flow), True)],
        ),
    ]
    return Report(
        repo=data.repo,
        as_of=data.as_of,
        window=flow["window"],
        bullets=bullets,
        flow=flow,
        dora=dora,
        when=when,
        how_many=how_many,
        backtest=backtest,
        forecast_note=note,
        sections=sections,
    )


def write_site(report: Report, out_dir: Path) -> Path:
    """index.html plus the report dicts it was built from, in data/."""
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("flow", report.flow),
        ("dora", report.dora),
        ("forecast_when", report.when),
        ("forecast_how_many", report.how_many),
        ("backtest", report.backtest),
    ):
        if value is not None:
            write_report(value, data_dir / f"{name}.json")
    write_failures(failure_rows(report.dora), data_dir / "dora_failures.csv")
    page = out_dir / "index.html"
    page.write_text(render_page(report))
    return page


def render_page(report: Report, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(UTC)
    window = report.window
    start = pd.Timestamp(window["start"])
    last_day = pd.Timestamp(window["end"]) - pd.Timedelta(days=1)
    as_of = report.as_of.tz_convert("UTC")
    figures = {}
    sections = []
    for section in report.sections:
        cards = []
        for chart, wide in section.charts:
            plot_id = f"plot-{section.id}-{chart.key}"
            figures[plot_id] = json.loads(pio.to_json(chart.figure, validate=False))
            cards.append(_card(chart, plot_id, wide))
        sections.append(
            f'<section id="{section.id}">\n<h2>{html.escape(section.title)}</h2>\n'
            f'<p class="intro">{html.escape(section.intro)}</p>\n'
            f"{_tiles(section.tiles)}\n"
            f'<div class="charts">\n{"".join(cards)}</div>\n{section.after}</section>'
        )
    nav = " ".join(f'<a href="#{s.id}">{html.escape(s.title)}</a>' for s in report.sections)
    bullets = "".join(
        f"<li><strong>{html.escape(b.lead)}:</strong> {html.escape(b.text)}</li>"
        for b in report.bullets
    )
    repo = html.escape(report.repo)
    data_links = " · ".join(
        f'<a href="data/{name}">{name}</a>'
        for name, value in (
            ("flow.json", report.flow),
            ("dora.json", report.dora),
            ("dora_failures.csv", report.dora),
            ("forecast_when.json", report.when),
            ("forecast_how_many.json", report.how_many),
            ("backtest.json", report.backtest),
        )
        if value is not None
    )
    return PAGE.format(
        title=f"FlowLens · {repo}",
        repo=repo,
        repo_url=f"https://github.com/{repo}",
        period=f"{window['weeks']} whole weeks, {date_text(start)} to {date_text(last_day)}",
        as_of=f"{date_text(as_of)}, {as_of:%H:%M} UTC",
        bullets=bullets,
        nav=nav,
        sections="\n".join(sections),
        data_links=data_links,
        metrics_url=METRICS_URL,
        project_url=PROJECT_URL,
        generated=f"{date_text(pd.Timestamp(generated_at))}, {generated_at:%H:%M} UTC",
        plotly=get_plotlyjs(),
        figures=_script_json(figures),
        dark=_script_json(charts.dark_colors()),
    )


# --- pieces -----------------------------------------------------------------------------


def _tiles(tiles: list[Tile]) -> str:
    if not tiles:
        return ""
    out = []
    for tile in tiles:
        band = ""
        if tile.band:
            source = f' title="Band limits: {html.escape(tile.source)}"' if tile.source else ""
            band = (
                f'<span class="band band-{BAND_MARKS[tile.band]}"{source}>'
                f'<span class="dot" aria-hidden="true"></span>{html.escape(tile.band)}</span>'
            )
        out.append(
            f'<div class="tile"><div class="tile-label">{html.escape(tile.label)}</div>'
            f'<div class="tile-value">{html.escape(tile.value)}</div>{band}'
            f'<div class="tile-note">{html.escape(tile.note)}</div></div>'
        )
    return f'<div class="tiles">{"".join(out)}</div>'


def _card(chart: Chart, plot_id: str, wide: bool) -> str:
    height = int(chart.figure.layout.height or 340)
    table = ""
    if len(chart.table):
        table = (
            f"<details><summary>Table: {len(chart.table):,} rows</summary>"
            f"{_table(chart.table)}</details>"
        )
    return (
        f'<figure class="card{" wide" if wide else ""}">'
        f"<h3>{html.escape(chart.title)}</h3>"
        f'<div class="plot" id="{plot_id}" style="height:{height}px" role="img" '
        f'aria-label="{html.escape(chart.caption)}"></div>'
        f"<figcaption>{html.escape(chart.caption)}</figcaption>{table}</figure>\n"
    )


def _table(frame: pd.DataFrame, max_rows: int = TABLE_ROWS) -> str:
    shown = frame.head(max_rows)
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in shown.columns)
    rows = []
    for record in shown.itertuples(index=False):
        cells = []
        for value in record:
            text = _cell(value)
            if isinstance(value, str) and value.startswith("https://"):
                text = f'<a href="{html.escape(value)}">link</a>'
            cells.append(f"<td>{text}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    more = ""
    if len(frame) > max_rows:
        more = f'<p class="note">First {max_rows} of {len(frame):,} rows.</p>'
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>{more}"
    )


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, list):
        return html.escape(", ".join(str(v) for v in value))
    return html.escape(str(value))


def _failed_deploys(dora: dict[str, Any]) -> str:
    table = charts.failed_deploys_table(failure_rows(dora))
    if table.empty:
        return (
            '<h3 class="table-title">Failed deployments</h3><p class="note">None in the window.</p>'
        )
    rows = []
    for row in table.to_dict("records"):
        signal = f'<a href="{html.escape(row["URL"])}">{html.escape(row["Signal"])}</a>'
        cells = [
            row["Deploy"],
            row["Deployed"],
            None,
            row["Title"],
            row["Opened after"],
            row["Recovered"],
            row["Recovered via"] or "",
        ]
        rows.append(
            "<tr>"
            + "".join(f"<td>{signal if c is None else html.escape(c)}</td>" for c in cells)
            + "</tr>"
        )
    return (
        '<h3 class="table-title">Failed deployments</h3>'
        '<p class="note">Every failure signal that counted, and what fixed it. The same rows '
        'are in <a href="data/dora_failures.csv">dora_failures.csv</a>.</p>'
        '<div class="table-wrap"><table class="failed"><thead><tr><th>Deploy</th><th>Deployed</th>'
        "<th>Signal</th><th>Title</th><th>Opened after</th><th>Recovered in</th>"
        f"<th>Recovered via</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _band_sources(dora: dict[str, Any]) -> str:
    if not dora["bands"]:
        return '<p class="note">No bands: add dora.bands to flowlens.yaml to rate each metric.</p>'
    sources: dict[str, list[str]] = {}
    for key, band in dora["bands"].items():
        sources.setdefault(band["source"], []).append(key.replace("_", " "))
    items = "".join(
        f"<li>{html.escape(', '.join(keys))}: {html.escape(source)}</li>"
        for source, keys in sources.items()
    )
    return (
        f'<p class="note">Band limits live in flowlens.yaml, from:</p><ul class="note">{items}</ul>'
    )


def _notes(notes: list[str]) -> str:
    if not notes:
        return ""
    items = "".join(f"<li>{html.escape(note)}</li>" for note in notes)
    return f'<h3 class="table-title">Caveats</h3><ul class="note">{items}</ul>'


def _script_json(value: Any) -> str:
    """JSON that is safe inside a <script> element."""
    return json.dumps(value, separators=(",", ":")).replace("</", "<\\/")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="Flow metrics, Monte Carlo forecasts, and DORA metrics for \
{repo}, from its GitHub history.">
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --hairline: #e1e0d9; --border: rgba(11,11,11,0.10); --link: #1c5cab;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --hairline: #2c2c2a; --border: rgba(255,255,255,0.10); --link: #86b6ef;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
a {{ color: var(--link); }}
main {{ max-width: 1120px; margin: 0 auto; padding: 32px 16px 64px; }}
header .eyebrow {{ margin: 0; color: var(--ink-2); font-size: 14px; letter-spacing: 0.02em; }}
h1 {{ margin: 4px 0 8px; font-size: clamp(28px, 5vw, 40px); line-height: 1.15;
  overflow-wrap: anywhere; }}
h1 a {{ color: inherit; text-decoration: none; }}
.meta {{ margin: 0; color: var(--ink-2); font-size: 15px; }}
.summary {{ margin: 24px 0 8px; padding: 20px 24px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 12px; }}
.summary h2 {{ margin: 0 0 8px; font-size: 18px; }}
.summary ul {{ margin: 0; padding-left: 20px; }}
.summary li {{ margin: 8px 0; }}
nav {{ position: sticky; top: 0; z-index: 2; display: flex; gap: 20px; overflow-x: auto;
  margin: 16px -16px 0; padding: 12px 16px; background: var(--page);
  border-bottom: 1px solid var(--hairline); font-size: 15px; white-space: nowrap; }}
nav a {{ text-decoration: none; font-weight: 600; }}
section {{ padding-top: 32px; scroll-margin-top: 48px; }}
h2 {{ margin: 0 0 4px; font-size: 26px; }}
.intro {{ margin: 0 0 16px; max-width: 72ch; color: var(--ink-2); }}
.tiles {{ display: grid; gap: 12px; margin-bottom: 16px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr)); }}
.tile {{ padding: 14px 16px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; }}
.tile-label {{ color: var(--ink-2); font-size: 14px; }}
.tile-value {{ margin: 2px 0; font-size: 26px; font-weight: 600; line-height: 1.2; }}
.tile-note {{ color: var(--ink-2); font-size: 13px; }}
.band {{ display: inline-flex; align-items: center; gap: 6px; margin: 2px 0 4px;
  padding: 1px 10px 1px 8px; border: 1px solid var(--border); border-radius: 999px;
  font-size: 13px; font-weight: 600; text-transform: capitalize; cursor: help; }}
.band .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.band-good .dot {{ background: var(--good); }}
.band-warning .dot {{ background: var(--warning); }}
.band-critical .dot {{ background: var(--critical); }}
.charts {{ display: grid; gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 480px), 1fr)); }}
.card {{ min-width: 0; margin: 0; padding: 16px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 12px; }}
.card.wide {{ grid-column: 1 / -1; }}
.card h3 {{ margin: 0 0 8px; font-size: 17px; }}
.plot {{ width: 100%; }}
figcaption {{ margin-top: 8px; color: var(--ink-2); font-size: 14px; max-width: 80ch; }}
details {{ margin-top: 8px; font-size: 14px; }}
summary {{ cursor: pointer; color: var(--ink-2); }}
.table-title {{ margin: 24px 0 4px; font-size: 17px; }}
.note {{ color: var(--ink-2); font-size: 14px; }}
ul.note {{ margin-top: 0; padding-left: 20px; }}
.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px;
  font-variant-numeric: tabular-nums; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid var(--hairline); text-align: left;
  vertical-align: top; }}
th {{ color: var(--ink-2); font-weight: 600; white-space: nowrap; }}
tr:last-child td {{ border-bottom: 0; }}
.failed td:nth-child(-n+3), .failed td:nth-child(5), .failed td:nth-child(6) {{
  white-space: nowrap; }}
footer {{ margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--hairline);
  color: var(--ink-2); font-size: 14px; }}
@media (max-width: 600px) {{
  main {{ padding-top: 20px; }}
  .summary {{ padding: 16px; }}
  .card {{ padding: 12px; }}
  .tile-value {{ font-size: 22px; }}
}}
</style>
</head>
<body>
<main>
<header>
<p class="eyebrow">FlowLens report</p>
<h1><a href="{repo_url}">{repo}</a></h1>
<p class="meta">{period} · data as of {as_of}</p>
</header>
<div class="summary">
<h2>Summary</h2>
<ul>{bullets}</ul>
</div>
<nav aria-label="Sections">{nav}</nav>
{sections}
<footer>
<p>Built by <a href="{project_url}">FlowLens</a> from the public GitHub history of \
<a href="{repo_url}">{repo}</a>. How each number is defined: \
<a href="{metrics_url}">METRICS.md</a>.</p>
<p>The data behind this page: {data_links}. Generated {generated}.</p>
</footer>
</main>
<script>{plotly}</script>
<script>
(() => {{
  const FIGURES = {figures};
  const DARK = {dark};
  const CONFIG = {{ responsive: true, displayModeBar: false }};
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const swap = (value) => {{
    if (typeof value === "string") return DARK[value] ?? value;
    if (Array.isArray(value)) return value.map(swap);
    if (value && typeof value === "object") {{
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, swap(v)]));
    }}
    return value;
  }};
  const draw = () => {{
    for (const [id, figure] of Object.entries(FIGURES)) {{
      const element = document.getElementById(id);
      if (!element) continue;
      const theme = media.matches ? swap : structuredClone;
      Plotly.react(element, theme(figure.data), theme(figure.layout), CONFIG);
    }}
  }};
  draw();
  media.addEventListener("change", draw);
}})();
</script>
</body>
</html>
"""
