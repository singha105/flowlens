import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from fakes import fill_with_streamlit
from flowlens import cli
from flowlens.ingest.client import GitHubError
from flowlens.ingest.runner import IngestSummary, RangeTally
from flowlens.storage import Store, db_path_for

runner = CliRunner()


def summary(**overrides) -> IngestSummary:
    oct_1 = datetime(2025, 10, 1, tzinfo=UTC)
    values = {
        "repo": "pallets/markupsafe",
        "db_path": Path("data/pallets__markupsafe.duckdb"),
        "mode": "full",
        "since": {
            "issues": oct_1,
            "pull_requests": oct_1,
            "releases": oct_1,
            "deployments": oct_1,
            "workflow_runs:Build Release": oct_1,
        },
        "fetched": {
            "issues": 42,
            "pull_requests": 1234,
            "releases": 14,
            "deployments": 0,
            "workflow_runs:Build Release": 77,
        },
        "first_updated": datetime(2025, 10, 2, tzinfo=UTC),
        "last_updated": datetime(2026, 10, 28, tzinfo=UTC),
        "api_calls": 27,
        "api_cost": 30,
        "rate_remaining": 4970,
        "rate_limit": 5000,
        "seconds": 3.42,
        "stored": {"issues": 42, "pull_requests": 1234},
    }
    return IngestSummary(**(values | overrides))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-10-01", datetime(2025, 10, 1, tzinfo=UTC)),
        ("2025-10-01T08:30:00Z", datetime(2025, 10, 1, 8, 30, tzinfo=UTC)),
        (
            "2025-10-01T08:30:00-04:00",
            datetime(2025, 10, 1, 8, 30, tzinfo=timezone(timedelta(hours=-4))),
        ),
        (None, None),
    ],
)
def test_parse_since(value, expected):
    parsed = cli.parse_since(value)

    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo == UTC


def test_parse_since_rejects_garbage():
    with pytest.raises(typer.BadParameter):
        cli.parse_since("last tuesday")


@pytest.fixture
def fake_ingest(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Replace the network part of `ingest`; the model is still built from tmp_path."""
    calls = {}

    def ingest(repo, **kwargs):
        calls.update(kwargs, repo=repo)
        return summary(repo=repo, db_path=tmp_path / "db.duckdb")

    monkeypatch.setattr(cli, "ingest_repo", ingest)
    monkeypatch.chdir(tmp_path)  # no ./flowlens.yaml: defaults
    return calls


def test_ingest_prints_a_summary(fake_ingest):
    result = runner.invoke(
        cli.app, ["ingest", "--repo", "pallets/markupsafe", "--since", "2025-10-01"]
    )

    assert result.exit_code == 0, result.output
    assert fake_ingest["repo"] == "pallets/markupsafe"
    assert fake_ingest["since"] == datetime(2025, 10, 1, tzinfo=UTC)
    assert fake_ingest["full"] is False
    assert fake_ingest["workflows"] == ()
    assert "pallets/markupsafe (full run)" in result.output
    assert (
        "pull requests               1,234 fetched  (since 2025-10-01 00:00 UTC)" in result.output
    )
    assert "runs of Build Release          77 fetched" in result.output
    assert "2025-10-02 → 2026-10-28" in result.output
    assert "27 (GraphQL cost 30, 4,970 of 5,000 points left)" in result.output
    assert "model          0 work items, 0 events, 0 deploys, 0 PRs matched" in result.output


def test_ingest_compares_deploys_from_the_configured_source(fake_ingest, tmp_path):
    (tmp_path / "flowlens.yaml").write_text("deployment_source: deployments\n")

    result = runner.invoke(cli.app, ["ingest", "--repo", "acme/shop"])

    assert result.exit_code == 0, result.output
    assert fake_ingest["config"].deployment_source == "deployments"


def test_summary_reports_the_deploy_ranges():
    ranges = RangeTally(
        source="releases", deploys=47, needed=46, fetched=3, commits=210, not_found=1
    )

    text = cli.format_summary(summary(ranges=ranges))

    assert (
        "  deploy ranges  3 compared (210 commits), 43 already stored, 1 unknown to GitHub; "
        "47 deploys from releases"
    ) in text.splitlines()
    assert (
        text.splitlines().index(next(line for line in text.splitlines() if "deploy ranges" in line))
        == next(i for i, line in enumerate(text.splitlines()) if "API calls" in line) - 1
    )


def test_ingest_passes_workflows_from_config_and_flags(fake_ingest, tmp_path):
    config = tmp_path / "custom.yaml"
    config.write_text("deployment_source: workflow:deploy.yml\nworkflows: [CI]\n")

    result = runner.invoke(
        cli.app,
        ["ingest", "--repo", "acme/shop", "--config", str(config), "--workflow", "CI",
         "--workflow", "Nightly"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert fake_ingest["workflows"] == ("CI", "deploy.yml", "Nightly")


def test_a_bad_config_exits_before_fetching(fake_ingest, tmp_path):
    (tmp_path / "flowlens.yaml").write_text("deployment_source: tags\n")

    result = runner.invoke(cli.app, ["ingest", "--repo", "acme/shop"])

    assert result.exit_code == 2
    assert "deployment_source" in result.output
    assert fake_ingest == {}


def test_summary_for_a_run_with_nothing_new():
    text = cli.format_summary(
        summary(
            mode="incremental",
            fetched={"issues": 0, "pull_requests": 0},
            since={"issues": None, "pull_requests": None},
            first_updated=None,
            last_updated=None,
        )
    )

    assert "nothing new" in text
    assert "(all history)" in text
    assert "model" not in text


def test_ingest_rejects_a_malformed_repo():
    result = runner.invoke(cli.app, ["ingest", "--repo", "markupsafe"])

    assert result.exit_code == 2
    assert "owner/name" in result.output


def test_ingest_reports_github_errors_without_a_traceback(monkeypatch: pytest.MonkeyPatch):
    def failing_ingest(*_args, **_kwargs):
        raise GitHubError("Could not resolve to a Repository")

    monkeypatch.setattr(cli, "ingest_repo", failing_ingest)

    result = runner.invoke(cli.app, ["ingest", "--repo", "nope/nope"])

    assert result.exit_code == 1
    assert "error: Could not resolve to a Repository" in result.output


def test_version():
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == "flowlens 0.1.0"


def test_quality_prints_the_report(tmp_path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    with Store(db_path_for(data_dir, "streamlit", "streamlit")) as store:
        fill_with_streamlit(
            store,
            coverage_from=datetime(2025, 10, 29, tzinfo=UTC),
            finished_at=datetime(2026, 10, 29, 5, tzinfo=UTC),
        )
    monkeypatch.chdir(Path(__file__).parent.parent)  # the repo's flowlens.yaml

    result = runner.invoke(
        cli.app, ["quality", "--repo", "streamlit/streamlit", "--data-dir", str(data_dir)]
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("Data quality · streamlit/streamlit · 2025-10-29")
    assert "Work items: 15 | with started_at: 69%" in result.output
    assert "Findings:\n  ! Only 56% of issues" in result.output


def test_quality_without_data_says_to_ingest_first(tmp_path):
    result = runner.invoke(cli.app, ["quality", "--repo", "acme/shop", "--data-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "Run `flowlens ingest` first" in result.output


def test_flow_prints_the_summary_and_writes_json(tmp_path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    with Store(db_path_for(data_dir, "streamlit", "streamlit")) as store:
        fill_with_streamlit(
            store,
            coverage_from=datetime(2025, 10, 29, tzinfo=UTC),
            finished_at=datetime(2026, 10, 29, 5, tzinfo=UTC),
        )
    monkeypatch.chdir(Path(__file__).parent.parent)

    result = runner.invoke(
        cli.app,
        ["flow", "--repo", "streamlit/streamlit", "--weeks", "52", "--data-dir", str(data_dir)],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith(
        "FlowLens flow · streamlit/streamlit · 52 weeks, 2025-10-27 → 2026-10-25 "
        "(as of 2026-10-29 05:00 UTC)\n"
    )
    out = data_dir / "out" / "flow_streamlit__streamlit.json"
    assert result.output.endswith(f"\nWrote {out}\n")
    report = json.loads(out.read_text())
    assert (report["repo"], report["window"]["weeks"]) == ("streamlit/streamlit", 52)
    assert len(report["throughput"]["weekly"]) == 52


def test_flow_without_data_says_to_ingest_first(tmp_path):
    result = runner.invoke(cli.app, ["flow", "--repo", "acme/shop", "--data-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "Run `flowlens ingest` first" in result.output


def test_flow_needs_at_least_one_week(tmp_path):
    result = runner.invoke(
        cli.app, ["flow", "--repo", "acme/shop", "--weeks", "0", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 2


@pytest.fixture
def streamlit_data(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    with Store(db_path_for(data_dir, "streamlit", "streamlit")) as store:
        fill_with_streamlit(
            store,
            coverage_from=datetime(2025, 10, 29, tzinfo=UTC),
            finished_at=datetime(2026, 10, 29, 5, tzinfo=UTC),
        )
    monkeypatch.chdir(Path(__file__).parent.parent)
    return data_dir


def test_forecast_when(streamlit_data):
    result = runner.invoke(
        cli.app,
        ["forecast", "--repo", "streamlit/streamlit", "--items", "2",
         "--data-dir", str(streamlit_data)],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Forecast: 2 issues from 2026-11-02 (12 weeks of history, 10,000 trials)"
    assert lines[1] == (
        "  history: 2026-08-03 → 2026-10-25, 0.2 issues/week (min 0, max 1, 10 empty weeks)"
    )
    out = streamlit_data / "out" / "forecast_streamlit__streamlit_issue.json"
    assert json.loads(out.read_text())["when"]["items"] == 2
    assert lines[-1] == f"Wrote {out}"


def test_forecast_how_many(streamlit_data):
    result = runner.invoke(
        cli.app,
        ["forecast", "--repo", "streamlit/streamlit", "--by", "2026-12-31",
         "--start", "2026-11-02", "--data-dir", str(streamlit_data)],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert result.output.startswith(
        "Forecast: issues done from 2026-11-02 through 2026-12-27 (8 whole weeks; "
    )


def test_forecast_needs_one_question(streamlit_data):
    for extra in ([], ["--items", "3", "--by", "2026-12-31"]):
        result = runner.invoke(
            cli.app,
            ["forecast", "--repo", "streamlit/streamlit", "--data-dir", str(streamlit_data),
             *extra],
        )  # fmt: skip
        assert result.exit_code == 2
        assert "give either --items" in result.output


def test_forecast_without_throughput_explains_why(streamlit_data):
    result = runner.invoke(
        cli.app,
        ["forecast", "--repo", "streamlit/streamlit", "--items", "5", "--type", "pr",
         "--data-dir", str(streamlit_data)],
    )  # fmt: skip

    assert result.exit_code == 1
    assert "nothing was delivered in the 12 weeks before 2026-11-02" in result.output


def test_backtest_scores_what_history_allows(streamlit_data):
    result = runner.invoke(
        cli.app,
        ["backtest", "--repo", "streamlit/streamlit", "--trials", "500",
         "--data-dir", str(streamlit_data)],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert result.output.startswith(
        "Backtest · streamlit/streamlit · issues · 500 trials per forecast "
        "(51 weeks of measured history from 2025-11-03)"
    )
    # 51 weeks leave room for 22 origins at 4 weeks out and 18 at 8 (26 weeks of history).
    assert "4-week horizon: 22 weekly origins" in result.output
    assert "8-week horizon: 18 weekly origins" in result.output
    out = streamlit_data / "out" / "backtest_streamlit__streamlit_issue.json"
    assert len(json.loads(out.read_text())["forecasts"]) == (22 + 18) * 3


def test_dora_prints_the_four_keys_and_writes_the_trace(streamlit_data):
    result = runner.invoke(
        cli.app, ["dora", "--repo", "streamlit/streamlit", "--data-dir", str(streamlit_data)]
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == ("DORA (26 weeks, 2026-04-27 → 2026-10-25, deployment source: releases)")
    # Five recorded releases, 1.63.0 to 1.66.1; the band comes from ./flowlens.yaml.
    assert lines[1].startswith("Deployment frequency: 0.19/week (median gap 11.8d)")
    assert lines[1].endswith("band: low")
    assert "first deploy: nothing to compare with" in result.output  # 1.63.0
    assert "not fetched: run flowlens ingest" in result.output  # no compares recorded
    assert "DORA 2024 Accelerate State of DevOps Report" in result.output
    report = json.loads((streamlit_data / "out" / "dora_streamlit__streamlit.json").read_text())
    assert [d["name"] for d in report["deploys"]] == [
        "1.63.0", "1.64.0", "1.65.0", "1.66.0", "1.66.1",
    ]  # fmt: skip
    failures = streamlit_data / "out" / "dora_failures_streamlit__streamlit.csv"
    assert failures.read_text().startswith("deploy,deployed_at,kind,signal,")
    assert (
        lines[-1]
        == f"Wrote {streamlit_data / 'out' / 'dora_streamlit__streamlit.json'} and {failures}"
    )


def test_dora_without_data_says_to_ingest_first(tmp_path):
    result = runner.invoke(cli.app, ["dora", "--repo", "acme/shop", "--data-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "Run `flowlens ingest` first" in result.output
