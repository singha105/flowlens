"""The flow report: its JSON shape, its notes, and the printed summary."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from fakes import fill_with_streamlit
from flowlens.config import load_config
from flowlens.metrics import flow
from flowlens.metrics.flow import Window
from flowlens.metrics.report import flow_report, flow_report_for, format_flow, write_report
from flowlens.model.build import build_model
from flowlens.storage import Store
from synthetic import Scenario, Synthetic, generate, work_item
from test_flow import FOUR_WEEKS, at


def small_team() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Three finished issues, one in progress, and one merged PR with every stage."""
    items = Synthetic(
        Scenario(),
        [
            work_item(1, created=at(0), started=at(1), done=at(3), kinds=["bug"]),
            work_item(2, created=at(0), started=at(2), done=at(9)),
            work_item(3, created=at(5), done=at(6)),  # closed without starting
            work_item(4, created=at(10), started=at(11)),
            work_item(5, created=at(3, 1), started=at(3), done=at(4, 3), type_="pr"),
        ],
    ).frame()
    prs = flow.pull_request_frame(
        [
            {
                "number": 5,
                "first_commit_at": at(3),
                "created_at": at(3, 1),
                "first_review_at": at(3, 21),
                "first_approval_at": at(4, 1),
                "merged_at": at(4, 3),
            }
        ]
    )
    return items, prs


def test_report_shape_for_a_synthetic_history():
    history = generate(seed=5, scenario=Scenario(days=185))
    window = Window.ending(history.as_of, 20)

    report = flow_report(history.frame(), history.pr_frame(), repo="acme/widgets", window=window)

    assert report["window"] == {
        "weeks": 20,
        "start": "2026-02-16T00:00:00Z",
        "end": "2026-07-06T00:00:00Z",
        "history_from": None,
    }
    assert report["as_of"] == "2026-07-09T00:00:00Z"
    assert set(report["lead_time_days"]) == {"issue", "pr"}
    assert set(report["cycle_time_days"]["issue"]) == {"all", "bug", "incident", "hotfix", "other"}
    weekly = report["throughput"]["weekly"]
    assert len(weekly) == 20
    assert weekly[0] == {"week": "2026-02-16T00:00:00Z", "iso_week": "2026-W08", **{
        k: weekly[0][k] for k in ("issue", "pr", "total")
    }}  # fmt: skip
    days = len(window.days())
    assert len(report["wip"]["daily"]) == days
    assert {name: len(rows) for name, rows in report["cumulative_flow"].items()} == {
        "issue": days,
        "pr": days,
        "total": days,
    }
    assert report["wip"]["today"] == {
        k: report["wip"]["daily"][-1][k] for k in ("issue", "pr", "total")
    }
    assert report["aging_wip"]["over_p85"] == sum(
        i["over_p85"] for i in report["aging_wip"]["items"]
    )
    assert sum(m["merged"] for m in report["pr_flow"]["monthly"]) == report["pr_flow"]["merged"]
    coverage = report["coverage"]["issue"]
    assert coverage["delivered"] == report["lead_time_days"]["issue"]["all"]["count"]
    assert coverage["with_start"] == report["cycle_time_days"]["issue"]["all"]["count"]
    assert report["notes"][0].startswith(
        f"Issue cycle time covers {coverage['with_start']:,} of {coverage['delivered']:,}"
    )
    json.dumps(report, allow_nan=False)  # no NaN, no numpy types, no timestamps


def test_report_with_no_items_is_still_valid_json():
    report = flow_report(
        flow.work_item_frame([]), flow.pull_request_frame([]), repo="acme/empty", window=FOUR_WEEKS
    )

    assert report["wip"]["today"] == {"issue": 0, "pr": 0, "total": 0}
    assert report["aging_wip"] == {"over_p85": 0, "items": []}
    assert report["pr_flow"]["bottleneck"] is None and report["pr_flow"]["monthly"] == []
    assert report["notes"] == []
    json.dumps(report, allow_nan=False)
    assert "Throughput  issues 0.0/week (min 0, max 0)" in format_flow(report)


def test_report_notes():
    items, prs = small_team()
    unreviewed = flow.pull_request_frame(
        prs.drop(columns=["first_review_at", "first_approval_at"]).to_dict("records")
    )

    report = flow_report(
        items,
        unreviewed,
        repo="acme/widgets",
        window=FOUR_WEEKS,
        history_from=datetime(2026, 3, 9, tzinfo=UTC),
    )

    assert report["notes"] == [
        "Issue cycle time covers 2 of 3 issues delivered (67%); the rest closed with no start "
        "signal (no assignment, linked PR or in-progress label).",
        "1 of 1 merged PRs had no human review before merging; they're left out of the review "
        "stages.",
        "The window starts 2026-03-02 but history was ingested from 2026-03-09; early weeks "
        "undercount.",
        "Items not updated since 2026-03-09 aren't in the data, so long-idle open items are "
        "missing from WIP and aging.",
    ]


def test_format_flow():
    items, prs = small_team()
    report = flow_report(items, prs, repo="acme/widgets", window=FOUR_WEEKS)

    assert format_flow(report).splitlines() == [
        "FlowLens flow · acme/widgets · 4 weeks, 2026-03-02 → 2026-03-29 "
        "(as of 2026-03-30 12:00 UTC)",
        "",
        "                               n     p50     p85     p95",
        "Cycle time  issues             2    2.0d    7.0d    7.0d",
        "              bug              1    2.0d    2.0d    2.0d",
        "              other            1    7.0d    7.0d    7.0d",
        "            PRs                1    1.1d    1.1d    1.1d",
        "Lead time   issues             3    3.0d    9.0d    9.0d",
        "              bug              1    3.0d    3.0d    3.0d",
        "              other            2    1.0d    9.0d    9.0d",
        "            PRs                1    1.1d    1.1d    1.1d",
        "",
        "Throughput  issues 0.8/week (min 0, max 2) | PRs 0.2/week (min 0, max 1) "
        "| weeks with none: 2",
        "WIP today   1 (issues 1, PRs 0) | aging > p85: 1 item (oldest issue #4, 18 days)",
        "PR flow     medians: first commit→open 1h | open→first review 20h | "
        "review→approve 4h | approve→merge 2h",
        "Bottleneck  waiting for first review (74% of the summed stage medians)",
        "",
        "  ! Issue cycle time covers 2 of 3 issues delivered (67%); the rest closed with no "
        "start signal (no assignment, linked PR or in-progress label).",
    ]


def test_short_durations_print_in_hours_and_minutes():
    items = Synthetic(
        Scenario(),
        [
            work_item(1, created=at(1), started=at(1, 0.25), done=at(1, 0.5)),
            work_item(2, created=at(1), started=at(1), done=at(1, 9)),
        ],
    ).frame()

    text = format_flow(
        flow_report(items, flow.pull_request_frame([]), repo="a/b", window=FOUR_WEEKS)
    )

    assert "Cycle time  issues             2     15m      9h      9h" in text
    assert "medians: first commit→open - |" in text


def test_report_from_the_store_uses_the_last_ingest(tmp_path):
    repo = "streamlit/streamlit"
    config = load_config(Path(__file__).parent.parent / "flowlens.yaml", repo)
    with Store(tmp_path / "streamlit.duckdb") as store:
        fill_with_streamlit(
            store,
            coverage_from=datetime(2025, 10, 29, tzinfo=UTC),
            finished_at=datetime(2026, 10, 29, 5, tzinfo=UTC),
        )
        build_model(store, config, repo)
        report = flow_report_for(store, repo, weeks=52)
        (delivered,) = store.con.execute(
            "SELECT count(*) FROM work_items WHERE NOT excluded "
            "AND outcome IN ('completed', 'closed_no_reason', 'merged') "
            "AND done_at >= '2025-10-27' AND done_at < '2026-10-26'"
        ).fetchone()

    assert report["as_of"] == "2026-10-29T05:00:00Z"
    assert report["window"]["start"] == "2025-10-27T00:00:00Z"
    assert report["window"]["history_from"] == "2025-10-29T00:00:00Z"
    assert sum(w["total"] for w in report["throughput"]["weekly"]) == delivered > 0
    assert any("early weeks undercount" in note for note in report["notes"])


def test_write_report(tmp_path):
    report = {"repo": "acme/widgets", "values": [1, 2.5, None]}

    path = write_report(report, tmp_path / "out" / "flow_acme__widgets.json")

    assert json.loads(path.read_text()) == report


def test_write_report_refuses_nan(tmp_path):
    with pytest.raises(ValueError, match="Out of range float"):
        write_report({"p50": float("nan")}, tmp_path / "flow.json")
