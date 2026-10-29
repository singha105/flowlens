from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from flowlens import cli
from flowlens.ingest.github import GitHubError
from flowlens.ingest.runner import IngestSummary

runner = CliRunner()


def summary(**overrides) -> IngestSummary:
    values = {
        "repo": "pallets/markupsafe",
        "db_path": Path("data/pallets__markupsafe.duckdb"),
        "mode": "full",
        "issues_since": datetime(2025, 10, 1, tzinfo=UTC),
        "prs_since": datetime(2025, 10, 1, tzinfo=UTC),
        "issues_fetched": 42,
        "prs_fetched": 1234,
        "first_updated": datetime(2025, 10, 2, tzinfo=UTC),
        "last_updated": datetime(2026, 10, 28, tzinfo=UTC),
        "api_calls": 27,
        "api_cost": 30,
        "rate_remaining": 4970,
        "rate_limit": 5000,
        "seconds": 3.42,
        "issues_stored": 42,
        "prs_stored": 1234,
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


def test_ingest_prints_a_summary(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def fake_ingest(repo, **kwargs):
        calls.update(kwargs, repo=repo)
        return summary()

    monkeypatch.setattr(cli, "ingest_repo", fake_ingest)

    result = runner.invoke(
        cli.app, ["ingest", "--repo", "pallets/markupsafe", "--since", "2025-10-01"]
    )

    assert result.exit_code == 0, result.output
    assert calls["repo"] == "pallets/markupsafe"
    assert calls["since"] == datetime(2025, 10, 1, tzinfo=UTC)
    assert calls["full"] is False
    assert "pallets/markupsafe (full run)" in result.output
    assert "1,234 fetched" in result.output
    assert "2025-10-02 → 2026-10-28" in result.output
    assert "27 (rate-limit cost 30, 4,970 of 5,000 points left)" in result.output


def test_summary_for_a_run_with_nothing_new():
    text = cli.format_summary(
        summary(
            mode="incremental",
            issues_fetched=0,
            prs_fetched=0,
            first_updated=None,
            last_updated=None,
            issues_since=None,
        )
    )

    assert "nothing new" in text
    assert "(all history)" in text


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
