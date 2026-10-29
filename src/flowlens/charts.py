"""Plotly figures for the dashboard and the HTML report, each with a how-to-read caption.

Every builder takes a report dict (flow, forecast, backtest, DORA, as the CLI writes them)
or a frame from flowlens.snapshot and returns a `Chart`: the figure, a title, a caption
that says how to read it with the numbers that matter, and the rows behind it.

Color does one job per chart. PRs are blue and issues orange everywhere (a validated
pair, light and dark); a highlighted series against gray context marks what the caption
is about; status colors appear only where a mark is good or bad. Light tokens are below,
and `DARK` maps each to its dark-mode step for the HTML report.
"""

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from flowlens.metrics.flow import PR_STAGES
from flowlens.metrics.report import STAGE_NAMES

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"
CONTEXT = ("#d6d4cc", "#b4b2a9", "#8f8d86")  # gray steps for series that aren't the story

DARK = {
    SURFACE: "#1a1a19",
    INK: "#f0efec",
    INK_2: "#c3c2b7",
    GRID: "#2c2c2a",
    AXIS: "#383835",
    BLUE: "#3987e5",
    ORANGE: "#d95926",
    AQUA: "#199e70",
    CONTEXT[0]: "#4a4945",
    CONTEXT[1]: "#6b6a64",
    CONTEXT[2]: "#8f8d86",
}
OPACITIES = (0.18, 0.35, 0.55)

TYPE_COLORS = {"pr": BLUE, "issue": ORANGE, "total": AQUA}
NOUNS = {"pr": ("PR", "PRs"), "issue": ("issue", "issues"), "total": ("item", "items")}
PERCENTILE_DASHES = {50: "dot", 85: "dash", 95: "longdash"}
# Log-scale ticks for durations in days.
DAY_TICKS = {
    1 / 24: "1h",
    0.25: "6h",
    1: "1d",
    7: "1w",
    30: "1mo",
    90: "3mo",
    365: "1y",
    1095: "3y",
}
FLOOR_DAYS = 1 / 96  # 15 minutes: durations below sit on the floor of a log axis
TRAILING_WEEKS = 8
MIN_TRAILING_DEPLOYS = 3  # fewer deploys than this make the trailing rate noise
# Below this many items delivered per backtest horizon, most forecasts are "at least 0" or 1
# and hold almost by default, so calibration isn't judged.
MIN_BACKTEST_ITEMS = 10


@dataclass(frozen=True)
class Chart:
    key: str
    title: str
    caption: str
    figure: go.Figure
    table: pd.DataFrame  # the rows behind the chart: its table view


def alpha(color: str, opacity: float) -> str:
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({red},{green},{blue},{opacity:g})"


def dark_colors() -> dict[str, str]:
    """Every light color the figures use, mapped to its dark-mode step."""
    colors = dict(DARK)
    for light, dark in [*DARK.items(), *((c, c) for c in (MUTED, GOOD, WARNING, SERIOUS))]:
        for opacity in OPACITIES:
            colors[alpha(light, opacity)] = alpha(dark, opacity)
    return colors


# --- flow -------------------------------------------------------------------------------


def cycle_time_scatter(finished: pd.DataFrame, report: dict[str, Any], type_: str) -> Chart:
    singular, plural = NOUNS[type_]
    rows = finished[(finished["type"] == type_) & finished["cycle_days"].notna()]
    rows = rows.sort_values("done_at")
    stats = report["cycle_time_days"][type_]["all"]
    table = pd.DataFrame(
        {
            "number": rows["number"],
            "title": rows["title"],
            "finished": rows["done_at"].dt.strftime("%Y-%m-%d"),
            "cycle_days": rows["cycle_days"].round(2),
            "url": rows["url"],
        }
    )
    title = f"Cycle time: {plural}"
    fig = _base(380)
    if rows.empty:
        return Chart(
            f"cycle_{type_}",
            title,
            f"No {plural} with a start signal finished in this window.",
            _empty(fig, f"No {plural} finished"),
            table,
        )

    fig.add_scatter(
        x=rows["done_at"],
        y=rows["cycle_days"].clip(lower=FLOOR_DAYS),
        mode="markers",
        name=f"Finished {plural} ({len(rows):,})",
        marker={
            "size": 8,
            "color": alpha(TYPE_COLORS[type_], 0.55),
            "line": {"color": SURFACE, "width": 1},
        },
        customdata=np.stack([rows["number"], _clip_titles(rows["title"]), rows["cycle_days"]], -1),
        hovertemplate="#%{customdata[0]} %{customdata[1]}<br>finished %{x|%b %d, %Y}"
        "<br>%{customdata[2]:.1f} days from start to finish<extra></extra>",
    )
    window = report["window"]
    span = [pd.Timestamp(window["start"]), pd.Timestamp(window["end"])]
    _percentile_lines(fig, stats, span)
    _log_days(fig.layout.yaxis, "Start to finish (log scale)")
    fig.update_xaxes(title_text="Finished", range=span)
    caption = (
        f"Each dot is {_a(singular)} finished in the window, by finish date and time from "
        f"start to finish. 85% of dots sit on or below the dashed p85 line, so "
        f"{days_text(stats['p85'])} is a fair delivery promise for {_a(singular)}."
    )
    return Chart(f"cycle_{type_}", title, caption, fig, table)


def throughput_chart(report: dict[str, Any]) -> Chart:
    weekly = pd.DataFrame(report["throughput"]["weekly"])
    title = "Weekly throughput"
    table = weekly[["iso_week", "pr", "issue"]] if len(weekly) else pd.DataFrame()
    fig = _base(320)
    if weekly.empty:
        return Chart(
            "throughput", title, "No weeks in this window.", _empty(fig, "No weeks"), table
        )
    weeks = pd.to_datetime(weekly["week"])
    for type_ in ("pr", "issue"):
        plural = NOUNS[type_][1]
        fig.add_bar(
            x=weeks,
            y=weekly[type_],
            name=_cap(plural),
            marker={"color": TYPE_COLORS[type_], "line": {"color": SURFACE, "width": 2}},
            hovertemplate=f"%{{y}} {plural}<extra></extra>",
        )
    fig.update_layout(barmode="stack", hovermode="x unified", bargap=0.3)
    fig.update_xaxes(hoverformat="Week of %b %d, %Y", title_text="Week (Monday to Sunday, UTC)")
    fig.update_yaxes(title_text="Items delivered")
    summary = report["throughput"]["summary"]
    total = summary["total"]
    caption = (
        f"Each bar is a week's delivered items, PRs and issues stacked: "
        f"{summary['pr']['mean']:.0f} PRs and {summary['issue']['mean']:.0f} issues a week "
        f"on average, {total['min']} to {total['max']} items in a week. Forecasts resample "
        "these weeks, so a step change here is a warning for them."
    )
    return Chart("throughput", title, caption, fig, table)


def cumulative_flow_chart(report: dict[str, Any], type_: str) -> Chart:
    plural = NOUNS[type_][1]
    days = pd.DataFrame(report["cumulative_flow"][type_])
    title = f"Cumulative flow: {plural}"
    fig = _base(340)
    if days.empty:
        return Chart(f"cfd_{type_}", title, "No days in this window.", _empty(fig, "No days"), days)
    x = pd.to_datetime(days["day"])
    bands = (
        ("done", "Done", alpha(INK_2, 0.35)),
        ("in_progress", "In progress (WIP)", alpha(TYPE_COLORS[type_], 0.55)),
        ("todo", "To do", alpha(MUTED, 0.18)),
    )
    for column, name, fill in bands:
        fig.add_scatter(
            x=x,
            y=days[column],
            name=name,
            mode="lines",
            stackgroup="flow",
            line={"width": 0.5, "color": fill},
            fillcolor=fill,
            hovertemplate=f"{name}: %{{y:,}}<extra></extra>",
        )
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(hoverformat="%b %d, %Y", title_text="Day (state at the end of it)")
    fig.update_yaxes(title_text=_cap(plural))
    wip, done = days["in_progress"], days["done"]
    weeks = max(len(days) / 7, 1)
    caption = (
        f"Each band counts {plural} in that state at the end of each day: the colored band's "
        f"height is WIP ({wip.iloc[0]:,} at the start, {wip.iloc[-1]:,} at the end) and the "
        f"slope of done is throughput ({done.iloc[-1] / weeks:.0f} a week). A band that keeps "
        "widening means work starts faster than it finishes."
    )
    return Chart(f"cfd_{type_}", title, caption, fig, days)


def aging_wip_chart(report: dict[str, Any]) -> Chart:
    items = pd.DataFrame(report["aging_wip"]["items"])
    title = "Aging work in progress"
    fig = _base(400)
    if items.empty:
        return Chart(
            "aging", title, "Nothing is in progress.", _empty(fig, "Nothing in progress"), items
        )
    columns = {"pr": 0, "issue": 1}
    items["x"] = items["type"].map(columns) + items["number"].map(_jitter)
    items["y"] = items["age_days"].clip(lower=FLOOR_DAYS)
    groups = (
        ("Older than p85", items["over_p85"], CRITICAL),
        ("Older than p50", items["over_p50"] & ~items["over_p85"], WARNING),
        ("Younger than p50", ~items["over_p50"], CONTEXT[2]),
    )
    for name, selected, color in groups:
        rows = items[selected]
        fig.add_scatter(
            x=rows["x"],
            y=rows["y"],
            mode="markers",
            name=f"{name} ({len(rows):,})",
            marker={"size": 8, "color": color, "line": {"color": SURFACE, "width": 1}},
            customdata=np.stack(
                [
                    rows["number"],
                    _clip_titles(rows["title"]),
                    rows["type"].map(_type_word),
                    rows["started_at"].str[:10],
                    rows["age_days"],
                ],
                -1,
            ),
            hovertemplate="#%{customdata[0]} %{customdata[1]}<br>%{customdata[2]}, started "
            "%{customdata[3]}<br>%{customdata[4]:,.0f} days in progress<extra></extra>",
        )
    for type_, position in columns.items():
        stats = report["cycle_time_days"][type_]["all"]
        _percentile_lines(fig, stats, [position - 0.42, position + 0.42], legend=type_ == "pr")
    counts = items["type"].value_counts()
    fig.update_xaxes(
        tickvals=list(columns.values()),
        ticktext=[f"{_cap(NOUNS[t][1])} ({counts.get(t, 0):,})" for t in columns],
        range=[-0.6, 1.6],
        showgrid=False,
    )
    _log_days(fig.layout.yaxis, "In progress for (log scale)")
    over = int(items["over_p85"].sum())
    caption = (
        "Each dot is an item still in progress, by how long since it started, against the "
        f"cycle times of finished items of its type. {over:,} of {len(items):,} sit above "
        "their p85 line, older than 85% of finished items took: unblock or close those first."
    )
    table = items[["type", "number", "title", "started_at", "age_days", "over_p85", "url"]]
    return Chart("aging", title, caption, fig, table)


# --- forecast ---------------------------------------------------------------------------


def when_chart(report: dict[str, Any]) -> Chart:
    when = report["when"]
    unit = NOUNS[report["unit"]][1]
    dist = pd.DataFrame(when["distribution"])
    title = f"When will {when['items']:,} {unit} be done?"
    fig = _base(340)
    x = pd.to_datetime(dist["date"])
    fig.add_bar(
        x=x,
        y=dist["share"] * 100,
        name="Simulated futures",
        showlegend=False,
        marker={"color": TYPE_COLORS[report["unit"]]},
        customdata=dist["cumulative"] * 100,
        hovertemplate="Done before %{x|%b %d}: %{y:.1f}% of trials"
        "<br>%{customdata:.0f}% done by then<extra></extra>",
    )
    marks = _group_marks({int(c): pd.Timestamp(d) for c, d in when["dates"].items()})
    top = float(dist["share"].max() * 100) * 1.15
    for value, confidences in marks.items():
        label = _confidence_label(confidences)
        _vertical(fig, value, top, max(confidences), f"{label}: by {date_text(value)}")
    step = max(1, math.ceil(len(x) / 8))
    fig.update_xaxes(
        title_text="Done before (the Monday after the last week)",
        hoverformat="%b %d, %Y",
        tickvals=list(x[::step]),
        ticktext=[f"{d:%b} {d.day}" for d in x[::step]],
    )
    fig.update_yaxes(title_text="Share of simulated futures", ticksuffix="%", rangemode="tozero")
    history = report["history"]
    caption = (
        f"Each bar is the share of {report['trials']:,} simulated futures that finish the "
        f"{when['items']:,} {unit} in that week, resampling the last {history['weeks']} weeks "
        f"of throughput. 85% finish by {date_text(pd.Timestamp(when['dates']['85']))}: promise "
        "that date, not the tallest bar."
    )
    table = pd.DataFrame(
        {
            "done_before": x.dt.strftime("%Y-%m-%d"),
            "share": dist["share"].round(4),
            "cumulative": dist["cumulative"].round(4),
        }
    )
    return Chart("when", title, caption, fig, table)


def how_many_chart(report: dict[str, Any]) -> Chart:
    how = report["how_many"]
    unit = NOUNS[report["unit"]][1]
    dist = pd.DataFrame(how["distribution"])
    through = pd.Timestamp(how["through"])
    title = f"How many {unit} by {date_text(through)}?"
    fig = _base(340)
    fig.add_bar(
        x=dist["items"],
        y=dist["share"] * 100,
        name="Simulated futures",
        showlegend=False,
        marker={"color": TYPE_COLORS[report["unit"]]},
        customdata=dist["at_least"] * 100,
        hovertemplate=f"Exactly %{{x}} {unit}: %{{y:.1f}}% of trials"
        f"<br>%{{customdata:.0f}}% reach at least %{{x}}<extra></extra>",
    )
    marks = _group_marks({int(c): int(n) for c, n in how["at_least"].items()})
    top = float(dist["share"].max() * 100) * 1.15
    for value, confidences in marks.items():
        label = _confidence_label(confidences)
        _vertical(fig, value, top, max(confidences), f"{label}: at least {value:,}")
    fig.update_layout(bargap=0.1)
    fig.update_xaxes(title_text=f"{_cap(unit)} done")
    fig.update_yaxes(title_text="Share of simulated futures", ticksuffix="%", rangemode="tozero")
    caption = (
        f"Each bar is the share of simulated futures that deliver exactly that many {unit} "
        f"from {date_text(pd.Timestamp(report['start']))} through {date_text(through)}. "
        f"85% deliver at least {how['at_least']['85']:,}: commit to that and treat more as "
        "upside."
    )
    table = dist.round(4)
    return Chart("how_many", title, caption, fig, table)


def calibration_chart(report: dict[str, Any], horizon: int, history_weeks: int = 12) -> Chart:
    unit = NOUNS[report["unit"]][0]
    scores = pd.DataFrame(report["scores"])
    title = f"Backtest: did {horizon}-week {unit} forecasts hold?"
    fig = _base(360)
    scores = scores[scores["horizon_weeks"] == horizon] if len(scores) else scores
    if scores.empty:
        return Chart(
            "calibration",
            title,
            "Not enough history to backtest: it needs the longest history plus the horizon.",
            _empty(fig, "Not enough history"),
            scores,
        )
    fig.add_scatter(
        x=[45, 100],
        y=[45, 100],
        mode="lines",
        name="Held as often as promised",
        line={"color": MUTED, "width": 1.5, "dash": "dot"},
        hoverinfo="skip",
    )
    confidences = [50, 85, 95]
    others = iter(CONTEXT[1:])
    for row in scores.sort_values("history_weeks").itertuples():
        chosen = row.history_weeks == history_weeks
        color = TYPE_COLORS[report["unit"]] if chosen else next(others, CONTEXT[2])
        held = [getattr(row, f"held_{c}") * 100 for c in confidences]
        fig.add_scatter(
            x=confidences,
            y=held,
            mode="lines+markers",
            name=f"{row.history_weeks} weeks of history",
            line={"color": color, "width": 2.5 if chosen else 1.5},
            marker={
                "size": 9 if chosen else 7,
                "color": color,
                "line": {"color": SURFACE, "width": 2},
            },
            hovertemplate=f"{row.history_weeks} weeks of history: the %{{x}}% forecast held "
            f"%{{y:.0f}}% of {row.origins} times<extra></extra>",
        )
    low = min(40.0, float(scores[[f"held_{c}" for c in confidences]].min().min() * 100) - 5)
    fig.update_xaxes(
        title_text="Promised confidence",
        tickvals=confidences,
        ticktext=[f"{c}%" for c in confidences],
        range=[45, 100],
    )
    fig.update_yaxes(title_text="How often it held", ticksuffix="%", range=[low, 101])
    chosen = scores[scores["history_weeks"] == history_weeks]
    caption = (
        f"Each line is a history length, scored on {int(scores['origins'].iloc[0])} past weeks: "
        "points below the diagonal held less often than promised (overconfident)."
    )
    if len(chosen):
        held85, held95 = float(chosen["held_85"].iloc[0]), float(chosen["held_95"].iloc[0])
        delivered = float(chosen["mean_actual"].iloc[0])
        caption += (
            f" With {history_weeks} weeks of history the 85% forecast held {held85:.0%} of the time"
        )
        if delivered < MIN_BACKTEST_ITEMS:
            caption += (
                f", but only {delivered:.1f} {NOUNS[report['unit']][1]} were delivered per "
                f"{horizon} weeks on average: too few to judge the forecasts."
            )
        else:
            caption += f"{_calibration_advice(held85, held95)}."
    table = scores.drop(columns=["first_origin", "last_origin"]).round(3)
    return Chart("calibration", title, caption, fig, table)


# --- DORA -------------------------------------------------------------------------------


def deploys_chart(report: dict[str, Any]) -> Chart:
    deploys = _deploy_frame(report)
    weeks = pd.to_datetime([w["week"] for w in report["metrics"]["deployment_frequency"]["weekly"]])
    hours = report["metrics"]["change_failure_rate"]["failure_window_hours"]
    title = "Deploys per week"
    fig = _base(300)
    if deploys.empty:
        table = pd.DataFrame({"week": weeks.strftime("%Y-%m-%d"), "held": 0, "failed": 0})
    else:
        week = deploys["deployed_at"].dt.normalize() - pd.to_timedelta(
            deploys["deployed_at"].dt.day_of_week, "D"
        )
        counts = pd.crosstab(week, deploys["failed"]).reindex(weeks, fill_value=0)
        table = pd.DataFrame(
            {
                "week": weeks.strftime("%Y-%m-%d"),
                "held": counts.get(False, pd.Series(0, index=weeks)).to_numpy(),
                "failed": counts.get(True, pd.Series(0, index=weeks)).to_numpy(),
            }
        )
    series = (("held", "Held", CONTEXT[2]), ("failed", f"Failed within {hours:.0f}h", CRITICAL))
    for column, name, color in series:
        fig.add_bar(
            x=weeks,
            y=table[column],
            name=name,
            marker={"color": color, "line": {"color": SURFACE, "width": 2}},
            hovertemplate=f"{name}: %{{y}}<extra></extra>",
        )
    fig.update_layout(barmode="stack", hovermode="x unified", bargap=0.3)
    fig.update_xaxes(hoverformat="Week of %b %d, %Y")
    fig.update_yaxes(title_text="Deploys", dtick=1, rangemode="tozero")
    freq = report["metrics"]["deployment_frequency"]
    caption = (
        f"Each bar is a week's deploys from {report['deployment_source']}, red where a failure "
        f"signal followed within {hours:.0f} hours: {freq['per_week']:.2f} a week on average, "
        f"with {freq['weeks_without']} of {report['window']['weeks']} weeks empty."
    )
    return Chart("deploys", title, caption, fig, table)


def lead_time_trend(report: dict[str, Any]) -> Chart:
    changes = pd.DataFrame(report["changes"])
    title = "Lead time for changes, per deploy"
    fig = _base(300)
    if changes.empty:
        return Chart(
            "lead_time",
            title,
            "No PRs shipped in this window.",
            _empty(fig, "No PRs shipped"),
            changes,
        )
    per_deploy = (
        changes.groupby(["deployed_at", "deploy"], sort=True)
        .agg(prs=("pr", "size"), lead_days=("lead_days", _p50), waiting_days=("waiting_days", _p50))
        .reset_index()
    )
    x = pd.to_datetime(per_deploy["deployed_at"])
    series = (
        ("lead_days", "p50 lead time", BLUE),
        ("waiting_days", "p50 waiting to deploy", CONTEXT[2]),
    )
    for column, name, color in series:
        fig.add_scatter(
            x=x,
            y=per_deploy[column],
            mode="lines+markers",
            name=name,
            line={"color": color, "width": 2},
            marker={"size": 8, "color": color, "line": {"color": SURFACE, "width": 2}},
            customdata=np.stack([per_deploy["deploy"], per_deploy["prs"]], -1),
            hovertemplate=f"%{{customdata[0]}} (%{{customdata[1]}} PRs): {name} %{{y:.1f}} days"
            "<extra></extra>",
        )
    fig.update_yaxes(title_text="Days", rangemode="tozero")
    fig.update_xaxes(hoverformat="%b %d, %Y")
    share = report["metrics"]["lead_time"]["waiting_share"]
    caption = (
        "Each point is a deploy: the p50 lead time of the PRs it shipped, first commit to "
        "deploy, and the p50 of the part spent merged and waiting for it. Where the lines run "
        "close, release cadence sets lead time, not coding"
        + (f"; here waiting is {share:.0%} of all lead time." if share is not None else ".")
    )
    per_deploy["deployed_at"] = x.dt.strftime("%Y-%m-%d")
    return Chart("lead_time", title, caption, fig, per_deploy.round(2))


def failure_rate_trend(
    report: dict[str, Any],
    trailing: int = TRAILING_WEEKS,
    min_deploys: int = MIN_TRAILING_DEPLOYS,
) -> Chart:
    deploys = _deploy_frame(report)
    cfr = report["metrics"]["change_failure_rate"]
    weeks = pd.to_datetime([w["week"] for w in report["metrics"]["deployment_frequency"]["weekly"]])
    title = "Change failure rate"
    fig = _base(300)
    rows = []
    for week in weeks:
        end = week + pd.Timedelta(weeks=1)
        recent = (
            deploys[
                (deploys["deployed_at"] >= end - pd.Timedelta(weeks=trailing))
                & (deploys["deployed_at"] < end)
            ]
            if len(deploys)
            else deploys
        )
        total = len(recent)
        failed = int(recent["failed"].sum()) if total else 0
        rows.append(
            {
                "week": week,
                "deploys": total,
                "failed": failed,
                "rate": failed / total if total >= min_deploys else None,
            }
        )
    table = pd.DataFrame(rows)
    if cfr["rate"] is None:
        return Chart(
            "failure_rate", title, "No deploys in this window.", _empty(fig, "No deploys"), table
        )
    fig.add_scatter(
        x=table["week"] + pd.Timedelta(weeks=1),
        y=table["rate"] * 100,
        mode="lines+markers",
        name=f"Trailing {trailing} weeks",
        line={"color": CRITICAL, "width": 2},
        marker={"size": 7, "color": CRITICAL, "line": {"color": SURFACE, "width": 2}},
        customdata=np.stack([table["failed"], table["deploys"]], -1),
        hovertemplate="%{y:.0f}%: %{customdata[0]} of %{customdata[1]} deploys in the "
        f"{trailing} weeks to %{{x|%b %d}}<extra></extra>",
        connectgaps=False,
    )
    fig.add_scatter(
        x=[weeks[0], weeks[-1] + pd.Timedelta(weeks=1)],
        y=[cfr["rate"] * 100] * 2,
        mode="lines",
        name=f"Whole window ({cfr['rate']:.0%})",
        line={"color": INK_2, "width": 1.5, "dash": "dot"},
        hoverinfo="skip",
    )
    fig.update_yaxes(title_text="Deploys that failed", ticksuffix="%", range=[0, 105])
    caption = (
        f"The line is the share of deploys in the trailing {trailing} weeks followed within "
        f"{cfr['failure_window_hours']:.0f} hours by a revert, a hotfix or incident label, or a "
        f"failed status (blank with fewer than {min_deploys} deploys to go on): "
        f"{cfr['failed']} of {cfr['deploys']} deploys over the window. Quote it with its "
        "window: a longer one counts more failures."
    )
    table["week"] = table["week"].dt.strftime("%Y-%m-%d")
    return Chart("failure_rate", title, caption, fig, table)


def recovery_chart(report: dict[str, Any]) -> Chart:
    failures = report["failures"]
    as_of = pd.Timestamp(report["as_of"])
    title = "Failed deployment recovery time"
    fig = _base(300)
    rows = []
    for failure in failures:
        deployed = pd.Timestamp(failure["deployed_at"])
        recovered = failure["recovery_hours"] is not None
        hours = (
            failure["recovery_hours"] if recovered else (as_of - deployed) / pd.Timedelta(hours=1)
        )
        rows.append(
            {
                "deploy": failure["name"],
                "deployed_at": deployed.strftime("%Y-%m-%d"),
                "days": hours / 24,
                "recovered": recovered,
                "via": failure["recovered_via"] or failure["recovery_note"],
            }
        )
    table = pd.DataFrame(rows)
    if table.empty:
        return Chart(
            "recovery",
            title,
            "No failed deploys in this window.",
            _empty(fig, "No failed deploys"),
            table,
        )
    series = (
        (True, "Recovered", CONTEXT[2], "recovered in"),
        (False, f"Not recovered by {date_text(as_of)}", CRITICAL, "open for"),
    )
    for recovered, name, color, verb in series:
        rows_ = table[table["recovered"] == recovered]
        if rows_.empty:
            continue
        fig.add_bar(
            x=rows_["deploy"],
            y=rows_["days"],
            name=name,
            marker={"color": color},
            customdata=np.stack([rows_["deployed_at"], rows_["via"]], -1),
            hovertemplate=f"%{{x}} (%{{customdata[0]}}): {verb} %{{y:.1f}} days"
            "<br>%{customdata[1]}<extra></extra>",
        )
    median = report["metrics"]["recovery_time"]["median_hours"]
    if median is not None:
        fig.add_scatter(
            x=[table["deploy"].iloc[0], table["deploy"].iloc[-1]],
            y=[median / 24] * 2,
            mode="lines",
            name=f"Median {hours_text(median)}",
            line={"color": INK_2, "width": 1.5, "dash": "dot"},
            hoverinfo="skip",
        )
    fig.update_layout(bargap=0.55 if len(table) > 4 else 0.75)
    fig.update_xaxes(categoryorder="array", categoryarray=list(table["deploy"]), type="category")
    fig.update_yaxes(title_text="Days", rangemode="tozero")
    rec = report["metrics"]["recovery_time"]
    still = f"; {rec['open']} not recovered yet" if rec["open"] else ""
    caption = (
        "Each bar is a failed deploy, from release to the deploy that shipped its fix (or the "
        f"incident closing): median {hours_text(median)} over {rec['recovered']} "
        f"recovered{still}."
    )
    table["days"] = table["days"].round(2)
    return Chart("recovery", title, caption, fig, table)


def failed_deploys_table(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """failure_rows() as a table for people: one row per signal that counted."""
    return pd.DataFrame(
        [
            {
                "Deploy": row["deploy"],
                "Deployed": row["deployed_at"][:10],
                "Signal": row["signal"].replace("issue#", "issue #").replace("pr#", "PR #"),
                "Title": row["title"],
                "Opened after": hours_text(row["hours_after"]),
                "Recovered": hours_text(row["recovery_hours"])
                if row["recovery_hours"]
                else "not yet",
                "Recovered via": row["recovered_via"],
                "URL": row["url"],
            }
            for row in rows
        ],
        columns=[
            "Deploy",
            "Deployed",
            "Signal",
            "Title",
            "Opened after",
            "Recovered",
            "Recovered via",
            "URL",
        ],
    )


# --- PR flow ----------------------------------------------------------------------------


def pr_flow_chart(report: dict[str, Any]) -> Chart:
    pr_flow = report["pr_flow"]
    monthly = pd.DataFrame(pr_flow["monthly"])
    title = "Where PR time goes, by month"
    fig = _base(360)
    if monthly.empty:
        return Chart(
            "pr_flow", title, "No PRs merged in this window.", _empty(fig, "No PRs merged"), monthly
        )
    bottleneck = pr_flow["bottleneck"]
    months = pd.to_datetime(monthly["month"]).dt.strftime("%b %Y")
    context = iter(CONTEXT)
    for stage, *_ in PR_STAGES:
        highlighted = stage == bottleneck
        name = _cap(STAGE_NAMES[stage]) + (" (bottleneck)" if highlighted else "")
        fig.add_bar(
            x=months,
            y=monthly[stage],
            name=name,
            marker={
                "color": BLUE if highlighted else next(context),
                "line": {"color": SURFACE, "width": 2},
            },
            customdata=monthly["merged"],
            hovertemplate=f"{STAGE_NAMES[stage]}: %{{y:.1f}}h<extra></extra>",
        )
    fig.update_layout(
        barmode="stack", hovermode="x unified", bargap=0.45, legend={"traceorder": "normal"}
    )
    fig.update_xaxes(type="category")
    fig.update_yaxes(title_text="Median hours per stage")
    caption = (
        "Each bar is a month of merged PRs, split into the median hours each stage took "
        "(medians, so a bar isn't a typical PR's total)."
    )
    if bottleneck:
        caption += (
            f" {_cap(STAGE_NAMES[bottleneck])} is the bottleneck at "
            f"{pr_flow['bottleneck_share']:.0%} of the summed stage medians: shorten that first."
        )
    last = monthly.iloc[-1]
    if last["reviewed_share"] is not None and last["reviewed_share"] < 0.5:
        caption += (
            f" Only {last['reviewed_share']:.0%} of {months.iloc[-1]}'s PRs had a human review, "
            "so its review stages describe few PRs."
        )
    table = monthly.round(2)
    return Chart("pr_flow", title, caption, fig, table)


# --- text -------------------------------------------------------------------------------


def days_text(days: float | None) -> str:
    if days is None or (isinstance(days, float) and math.isnan(days)):
        return "-"
    if days < 1:
        return hours_text(days * 24)
    return f"{days:.1f} days" if days < 100 else f"{days:,.0f} days"


def hours_text(hours: float | None) -> str:
    if hours is None or (isinstance(hours, float) and math.isnan(hours)):
        return "-"
    if hours < 1:
        minutes = round(hours * 60)
        return {0: "under a minute", 1: "1 minute"}.get(minutes, f"{minutes} minutes")
    if hours < 48:
        return f"{hours:.0f} hours" if hours >= 1.5 else "1 hour"
    return days_text(hours / 24)


def date_text(value: pd.Timestamp) -> str:
    return f"{value:%b} {value.day}, {value.year}"


# --- helpers ----------------------------------------------------------------------------


def _base(height: int) -> go.Figure:
    fig = go.Figure()
    axis = {
        "showline": True,
        "linecolor": AXIS,
        "linewidth": 1,
        "gridcolor": GRID,
        "gridwidth": 1,
        "zeroline": False,
        "ticks": "outside",
        "tickcolor": AXIS,
        "ticklen": 4,
        "tickfont": {"color": MUTED, "size": 12},
        "title": {"font": {"color": INK_2, "size": 12}},
        "automargin": True,
    }
    fig.update_layout(
        template="none",
        height=height,
        margin={"l": 8, "r": 16, "t": 40, "b": 8},
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font={"family": FONT, "size": 13, "color": INK_2},
        hoverlabel={
            "bgcolor": SURFACE,
            "bordercolor": AXIS,
            "font": {"family": FONT, "color": INK, "size": 13},
        },
        legend={
            "orientation": "h",
            "x": 0,
            "xanchor": "left",
            "y": 1.02,
            "yanchor": "bottom",
            "font": {"size": 12, "color": INK_2},
            "bgcolor": "rgba(0,0,0,0)",
            "traceorder": "normal",
        },
        dragmode=False,
        barcornerradius=4,
        hovermode="closest",
    )
    fig.update_xaxes(**axis, showgrid=False)
    fig.update_yaxes(**axis, showgrid=True)
    return fig


def _empty(fig: go.Figure, text: str) -> go.Figure:
    fig.add_annotation(
        text=text,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": MUTED, "size": 14},
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(showlegend=False, height=160)
    return fig


def _percentile_lines(fig: go.Figure, stats: dict, span: list, legend: bool = True) -> None:
    for p, dash in PERCENTILE_DASHES.items():
        value = stats.get(f"p{p}")
        if value is None:
            continue
        fig.add_scatter(
            x=span,
            y=[max(value, FLOOR_DAYS)] * 2,
            mode="lines",
            name=f"p{p}",
            legendgroup=f"p{p}",
            showlegend=legend,
            line={
                "color": INK if p == 85 else INK_2,
                "width": 2 if p == 85 else 1.5,
                "dash": dash,
            },
            hovertemplate=f"p{p}: {days_text(value)}<extra></extra>",
        )


def _log_days(axis: go.layout.YAxis, title: str) -> None:
    axis.update(
        type="log", title_text=title, tickvals=list(DAY_TICKS), ticktext=list(DAY_TICKS.values())
    )


def _vertical(fig: go.Figure, x: Any, top: float, confidence: int, name: str) -> None:
    fig.add_scatter(
        x=[x, x],
        y=[0, top],
        mode="lines",
        name=name,
        line={
            "color": INK if confidence == 85 else INK_2,
            "width": 2 if confidence == 85 else 1.5,
            "dash": PERCENTILE_DASHES[confidence],
        },
        hovertemplate=f"{name}<extra></extra>",
    )


def _group_marks(values: dict[int, Any]) -> dict[Any, list[int]]:
    """Confidence levels that land on the same value share one line and one label."""
    marks: dict[Any, list[int]] = {}
    for confidence, value in sorted(values.items()):
        marks.setdefault(value, []).append(confidence)
    return marks


def _confidence_label(confidences: list[int]) -> str:
    names = [f"{c}%" for c in confidences]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _calibration_advice(held85: float, held95: float) -> str:
    if held85 < 0.80:
        return f", so quote the 95% forecast (held {held95:.0%})"
    if held85 > 0.90:
        return ", a little cautious"
    return ", close to its promise"


def _deploy_frame(report: dict[str, Any]) -> pd.DataFrame:
    deploys = pd.DataFrame(report["deploys"], columns=["name", "deployed_at", "failed", "status"])
    deploys["deployed_at"] = pd.to_datetime(deploys["deployed_at"], utc=True)
    deploys["failed"] = deploys["failed"].astype(bool)
    return deploys


def _p50(values: pd.Series) -> float:
    return float(np.percentile(values, 50, method="inverted_cdf"))


def _jitter(number: int) -> float:
    """A fixed horizontal offset per item, so the dots of a column don't overlap."""
    return ((number * 2654435761) % 1000 / 1000 - 0.5) * 0.6


def _clip_titles(titles: pd.Series, width: int = 70) -> pd.Series:
    titles = titles.fillna("")
    return titles.where(titles.str.len() <= width, titles.str[: width - 1] + "…")


def _a(noun: str) -> str:
    return f"an {noun}" if noun[0] in "aeiou" else f"a {noun}"


def _cap(text: str) -> str:
    """Capitalize the first letter only, so "PRs" stays "PRs"."""
    return text[:1].upper() + text[1:]


def _type_word(type_: str) -> str:
    return NOUNS[type_][0]
