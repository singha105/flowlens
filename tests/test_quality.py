"""The model build and the data-quality report, over the recorded streamlit fixtures.

The fixtures hold 10 issues and 5 PRs picked for their edge cases, so every number below
can be checked by hand against tests/fixtures/streamlit_*.json.
"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import fill_with_streamlit, fixture_items
from flowlens.config import Config, load_config, parse_config
from flowlens.ingest.github import issue_row
from flowlens.model.build import build_model
from flowlens.quality import build_report, format_report
from flowlens.storage import Store

REPO = "streamlit/streamlit"
CONFIG = load_config(Path(__file__).parent.parent / "flowlens.yaml", REPO)
COVERAGE_FROM = datetime(2025, 10, 29, tzinfo=UTC)
FINISHED_AT = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "streamlit.duckdb") as store:
        fill_with_streamlit(store, coverage_from=COVERAGE_FROM, finished_at=FINISHED_AT)
        yield store


def items(store) -> dict[str, dict]:
    return {row["id"]: row for row in store.rows("work_items")}


# --- model build ------------------------------------------------------------------------


def test_build_model_writes_work_items_events_and_deploys(store):
    counts = build_model(store, CONFIG, REPO)

    assert (counts.work_items, counts.deploys) == (15, 5)
    assert counts.events == store.count("work_item_events")
    built = items(store)
    assert built["issue#11752"]["started_signal"] == "first_linked_pr"
    assert built["pr#17735"]["exclude_reason"] == "bot"
    assert built["issue#8793"]["reopen_count"] == 1


def test_rebuilding_applies_a_config_change_without_refetching(store):
    build_model(store, CONFIG, REPO)
    assert items(store)["issue#17733"]["kinds"] == ["bug", "incident"]

    build_model(store, Config(), REPO)  # default labels: "bug", not "type:bug"

    assert items(store)["issue#17733"]["kinds"] == []
    assert store.count("work_items") == 15  # replaced, not duplicated


def test_a_pr_closing_reference_links_an_issue_the_timeline_missed(store):
    # PR 17688 lists issue 17612 in its closing references; give the store that issue
    # with an empty timeline.
    node = fixture_items("streamlit_issues.json")[17733]
    store.upsert("issues", [issue_row(node) | {"number": 17612}])

    build_model(store, CONFIG, REPO)

    issue = items(store)["issue#17612"]
    assert issue["linked"] == [17688]
    assert issue["started_signal"] == "first_linked_pr"


def test_deploys_follow_the_configured_source(store):
    build_model(store, replace(CONFIG, deployment_source="workflow:Build Release"), REPO)

    deploys = store.rows("deploys")
    assert len(deploys) == 5
    assert sum(d["status"] == "failure" for d in deploys) == 2


# --- the report -------------------------------------------------------------------------


@pytest.fixture
def report(store):
    build_model(store, CONFIG, REPO)
    return build_report(store, CONFIG, REPO)


def test_report_counts(report):
    assert (report.work_items, report.issues, report.prs) == (15, 10, 5)
    assert report.excluded_by == {"state_reason:NOT_PLANNED": 1, "bot": 1}
    # Issues: 9 counted, 5 with a start (3 by assignment, 2 by a linked PR).
    assert (report.included_issues, report.issues_started) == (9, 5)
    assert report.issue_signals == {"first_assigned": 3, "first_linked_pr": 2}
    # 4 finished; 17733 closed without anyone being assigned or opening a PR.
    assert (report.issues_done, report.issues_done_without_start) == (4, 1)
    assert (report.issues_closed, report.issues_closed_no_reason) == (5, 0)
    # PRs: the bot's is excluded; 2 of the rest merged, and only 17730 had a human review.
    assert (report.included_prs, report.prs_merged, report.prs_merged_reviewed) == (4, 2, 1)
    assert report.prs_merged_approved == 1
    assert report.median_hours_to_first_review == pytest.approx(36 / 3600)
    assert (report.deploys, report.deploy_failures, report.releases) == (5, 0, 5)
    assert report.deployments_by_environment == {"nightly": 37, "release": 3}
    assert report.workflow_runs == {"Build Release": 6}
    assert report.window_start == COVERAGE_FROM
    assert report.window_end == FINISHED_AT


def test_report_findings_call_out_low_coverage(report):
    findings = report.findings

    assert len(findings) == 3
    assert findings[0].startswith("Only 56% of issues have a start signal")
    assert findings[1].startswith("1 of 4 finished issues (25%) closed without ever starting")
    assert findings[2].startswith("Fewer than one deploy a month from releases")


def test_report_text(report):
    text = format_report(report)

    assert text.splitlines()[0] == (
        "Data quality · streamlit/streamlit · 2025-10-29 → 2026-10-29 (12.0 months)"
    )
    assert "Work items: 15 | with started_at: 69% | closed not planned (excluded): 7%" in text
    assert "10 issues, 5 PRs; 2 excluded (bot authors 1, not planned 1)" in text
    assert "Issues: 9 counted | with started_at: 56% (first_assigned 33%, first_linked_pr 22%)" in (
        text
    )
    assert "closed without ever starting: 1 of 4 finished (25%)" in text
    assert "PRs: 4 counted, 2 merged | with a review: 50% | median time to first review: 1m" in (
        text
    )
    assert "Deployments (releases): 5 in 12 months (0.4 a month)" in text
    assert (
        "signals found: releases 5 · deployments 40 (nightly 37, release 3) · "
        "workflow 'Build Release' 6 runs"
    ) in text
    assert "\n  ! Only 56% of issues" in text


def test_no_deploys_points_at_the_signals_that_exist(store):
    config = parse_config({"deployment_source": "deployments"})  # production, prod: none
    build_model(store, config, REPO)

    findings = build_report(store, config, REPO).findings

    assert findings[-1] == (
        "No deploys found from deployments; DORA metrics need one. Signals found: "
        "releases (5), deployments to nightly (37), deployments to release (3), "
        "workflow:Build Release (6 runs)."
    )


def test_an_empty_database_reports_without_crashing(tmp_path):
    with Store(tmp_path / "empty.duckdb") as store:
        build_model(store, Config(), "acme/empty")
        report = build_report(store, Config(), "acme/empty")

    text = format_report(report)
    assert report.work_items == 0
    assert "no data" in text
    assert "with started_at: n/a" in text
    assert "No deploys found from releases" in text
