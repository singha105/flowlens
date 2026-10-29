from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from fakes import fill_with_streamlit
from flowlens import cli
from flowlens.ingest.client import GitHubError
from flowlens.ingest.runner import IngestSummary
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
    assert "model          0 work items, 0 events, 0 deploys" in result.output


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
