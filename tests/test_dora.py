"""DORA metrics on small synthetic histories whose answers are known by construction."""

import csv
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from flowlens.config import Band, Config, Dora
from flowlens.metrics.dora import (
    COUNTED,
    History,
    Issue,
    PullRequest,
    change_failure_rate,
    deployment_frequency,
    failure_signals,
    lead_time,
    not_shipped,
    recoveries,
    recovery_time,
    reverted_pr,
    shipped_changes,
)
from flowlens.metrics.dora_report import dora_report, failure_rows, format_dora, write_failures
from flowlens.metrics.flow import Window
from flowlens.model.changes import DeploymentChange
from flowlens.model.deploys import Deploy

WINDOW = Window.ending(datetime(2026, 6, 30, 12, tzinfo=UTC), 4)  # 2026-06-01 to 06-29
START = WINDOW.start.to_pydatetime()
H72 = timedelta(hours=72)


def at(days: float = 0, hours: float = 0) -> datetime:
    """A time inside the window, counted from its first Monday."""
    return START + timedelta(days=days, hours=hours)


class Repo:
    """A synthetic history built one deploy, PR, and issue at a time."""

    def __init__(self) -> None:
        self.deploys: list[Deploy] = []
        self.changes: dict[int, DeploymentChange] = {}
        self.prs: dict[int, PullRequest] = {}
        self.issues: list[Issue] = []

    def deploy(self, name: str, when: datetime, status: str = "success") -> Deploy:
        deploy = Deploy(name, "releases", name, f"sha-{name}", None, status, when, None)
        self.deploys.append(deploy)
        return deploy

    def pr(
        self,
        number: int,
        *,
        merged: datetime | None,
        created: datetime | None = None,
        first_commit: datetime | None = None,
        title: str | None = None,
        kinds: tuple[str, ...] = (),
        base: str = "main",
        shipped_in: Deploy | None = None,
        excluded: bool = False,
    ) -> PullRequest:
        created = created or (merged - timedelta(hours=1) if merged else at())
        pr = PullRequest(
            number=number,
            title=title or f"Change {number}",
            url=f"https://github.com/o/r/pull/{number}",
            created_at=created,
            merged_at=merged,
            first_commit_at=first_commit,
            merge_commit_sha=f"merge-{number}",
            base_ref_name=base,
            kinds=kinds,
            excluded=excluded,
        )
        self.prs[number] = pr
        if shipped_in is not None:
            self.changes[number] = DeploymentChange(
                number, shipped_in.id, f"merge-{number}", "merge_commit"
            )
        return pr

    def issue(
        self,
        number: int,
        *,
        created: datetime,
        closed: datetime | None = None,
        linked: tuple[int, ...] = (),
        kinds: tuple[str, ...] = ("incident",),
    ) -> Issue:
        issue = Issue(number, f"Issue {number}", None, created, closed, kinds, linked)
        self.issues.append(issue)
        return issue

    def history(self, messages: dict[str, str] | None = None) -> History:
        return History(self.deploys, self.changes, self.prs, self.issues, messages or {})


def failures(history: History, window: timedelta = H72):
    signals = failure_signals(history, window)
    cfr = change_failure_rate(history.deploys, signals, WINDOW)
    recovered = recoveries(history, signals)
    return signals, cfr, recovered


# --- deployment frequency ---------------------------------------------------------------


def test_weeks_with_no_deployments_count_as_zero():
    repo = Repo()
    repo.deploy("v0", at(-3))  # before the window
    repo.deploy("v1", at(1))
    repo.deploy("v2", at(3))
    repo.deploy("v3", at(15))
    repo.deploy("v4-bad", at(22), status="failure")  # never reached production

    freq = deployment_frequency(repo.deploys, WINDOW)

    assert [week["deploys"] for week in freq["weekly"]] == [2, 0, 1, 0]
    assert freq["deploys"] == 3
    assert freq["weeks_without"] == 2
    assert freq["per_week"] == 3 / 4
    assert freq["per_day"] == 3 / 28
    assert freq["median_gap_days"] == 2  # gaps of 2 and 12 days, nearest-rank


def test_no_deploys_means_no_gap():
    freq = deployment_frequency([], WINDOW)

    assert (freq["deploys"], freq["median_gap_days"], freq["weeks_without"]) == (0, None, 4)


# --- lead time --------------------------------------------------------------------------


def test_lead_time_runs_from_first_commit_to_the_deploy_that_shipped_it():
    repo = Repo()
    v1 = repo.deploy("v1", at(3))
    old = repo.deploy("v0", at(-5))
    repo.pr(1, first_commit=at(0), merged=at(1), shipped_in=v1)
    repo.pr(2, created=at(2), merged=at(2, 12), shipped_in=v1)  # no first commit: opened
    repo.pr(3, first_commit=at(0), merged=at(1), shipped_in=v1, excluded=True)  # a bot
    repo.pr(4, first_commit=at(-9), merged=at(-6), shipped_in=old)  # before the window

    changes = shipped_changes(repo.history(), WINDOW)
    summary = lead_time(changes)

    assert [c.pr.number for c in changes] == [1, 2]
    assert [c.lead / timedelta(days=1) for c in changes] == [3, 1]
    assert [c.coding / timedelta(days=1) for c in changes] == [1, 0.5]
    assert [c.waiting / timedelta(days=1) for c in changes] == [2, 0.5]
    assert (summary["p50_days"], summary["p85_days"]) == (1, 3)
    assert summary["waiting_share"] == 2.5 / 4
    assert summary["from_first_commit"] == 1


def test_a_pr_that_shipped_in_no_deployment_is_reported_not_measured():
    repo = Repo()
    v1 = repo.deploy("v1", at(3))
    repo.pr(1, first_commit=at(0), merged=at(1), shipped_in=v1)
    repo.pr(2, merged=at(5))  # on main after the last deploy: waiting for the next
    repo.pr(3, merged=at(2), base="feature/big-thing")  # reaches main through another PR
    repo.pr(4, merged=None)  # still open

    history = repo.history()
    pending = not_shipped(history, WINDOW)

    assert [c.pr.number for c in shipped_changes(history, WINDOW)] == [1]
    assert pending["main_line"] == "main"
    assert [pr.number for pr in pending["waiting"]] == [2]
    assert [pr.number for pr in pending["other_branches"]] == [3]


def test_before_anything_ships_the_main_line_is_where_most_prs_merge():
    repo = Repo()
    repo.deploy("v1", at(1))  # the first deploy: what it shipped is unknown
    repo.pr(1, merged=at(2))
    repo.pr(2, merged=at(3))
    repo.pr(3, merged=at(4), base="feature/x")

    pending = not_shipped(repo.history(), WINDOW)

    assert pending["main_line"] == "main"
    assert ([p.number for p in pending["waiting"]], len(pending["other_branches"])) == ([1, 2], 1)


# --- change failure rate and recovery ---------------------------------------------------


def test_a_revert_counts_against_the_deploy_that_shipped_what_it_reverts():
    repo = Repo()
    v1 = repo.deploy("v1", at(1))
    repo.deploy("v2", at(2))
    v3 = repo.deploy("v3", at(4))
    repo.pr(10, title="Add the thing", merged=at(0, 12), shipped_in=v1)
    repo.pr(
        11, title='Revert "Add the thing (#10)"', created=at(2, 6), merged=at(2, 8), shipped_in=v3
    )

    signals, cfr, recovered = failures(repo.history())

    (revert,) = signals
    assert (revert.kind, revert.ref, revert.deploy_id, revert.outcome) == (
        "revert", "pr#11", "v1", COUNTED,
    )  # fmt: skip
    assert revert.reason == "reverts #10, opened 30h after v1 shipped it"
    assert (cfr["failed"], cfr["deploys"], cfr["rate"]) == (1, 3, 1 / 3)
    assert recovered["v1"].hours(v1.deployed_at) == 72  # until v3 shipped the revert
    assert recovered["v1"].via == "v3"


def test_reverts_that_dont_mean_production_failed_are_kept_with_the_reason():
    repo = Repo()
    v1 = repo.deploy("v1", at(1))
    v2 = repo.deploy("v2", at(10))
    repo.pr(20, merged=at(0), shipped_in=v1)
    repo.pr(21, merged=at(9), shipped_in=v2)
    repo.pr(22, merged=at(9, 1))  # never shipped
    repo.pr(30, title="Revert #20", created=at(5), merged=at(5))  # 4 days after v1
    repo.pr(31, title="Revert PR-21", created=at(9, 2), merged=at(9, 3))  # before v2 shipped it
    repo.pr(32, title="Revert #22", created=at(9, 4), merged=at(9, 5))

    signals = failure_signals(repo.history(), H72)

    assert {s.ref: s.outcome for s in signals} == {
        "pr#30": "outside_window",
        "pr#31": "before_shipping",
        "pr#32": "never_shipped",
    }
    assert change_failure_rate(repo.deploys, signals, WINDOW)["failed"] == 0


def test_a_hotfix_chain_is_a_chain_of_failures_each_with_its_own_recovery():
    repo = Repo()
    v1 = repo.deploy("v1", at(1))
    v2 = repo.deploy("v2", at(1, 20))
    v3 = repo.deploy("v3", at(1, 50))
    repo.deploy("v4", at(10))
    # v1 breaks; hotfix 1 ships in v2, which breaks too; hotfix 2 ships in v3, which holds.
    repo.pr(40, kinds=("hotfix",), created=at(1, 10), merged=at(1, 18), shipped_in=v2)
    repo.pr(41, kinds=("hotfix",), created=at(1, 25), merged=at(1, 45), shipped_in=v3)

    signals, cfr, recovered = failures(repo.history())

    # Hotfix 2 is 25h after v1 too, but it counts against the latest deploy, v2.
    assert [(s.ref, s.deploy_id) for s in signals] == [("pr#40", "v1"), ("pr#41", "v2")]
    assert [d.name for d in cfr.pop("failed_deploys")] == ["v1", "v2"]
    assert (cfr["failed"], cfr["deploys"]) == (2, 4)
    assert recovered["v1"].hours(v1.deployed_at) == 20
    assert recovered["v2"].hours(v2.deployed_at) == 30
    summary = recovery_time([v1, v2], recovered)
    assert (summary["median_hours"], summary["recovered"], summary["open"]) == (20, 2, 0)


def test_a_failed_deployment_status_counts_and_recovers_at_the_next_success():
    repo = Repo()
    repo.deploy("v1", at(1))
    bad = repo.deploy("v2", at(2), status="failure")
    repo.deploy("v3", at(3))

    signals, cfr, recovered = failures(repo.history())

    assert [(s.kind, s.deploy_id) for s in signals] == [("failed_status", "v2")]
    assert (cfr["failed"], cfr["deploys"]) == (1, 3)
    assert deployment_frequency(repo.deploys, WINDOW)["deploys"] == 2
    assert recovered["v2"].hours(bad.deployed_at) == 24


def test_incidents_recover_when_their_fix_ships_or_when_they_close():
    repo = Repo()
    v1 = repo.deploy("v1", at(1))
    v2 = repo.deploy("v2", at(5))
    v3 = repo.deploy("v3", at(9))
    repo.pr(50, merged=at(3), shipped_in=v2)
    repo.pr(51, merged=at(10))  # merged, not deployed yet
    repo.issue(60, created=at(1, 5), linked=(50,), closed=at(3))  # fixed by #50, in v2
    repo.issue(61, created=at(5, 1), closed=at(5, 10))  # no linked PR: closing ends it
    repo.issue(62, created=at(9, 1), linked=(51,), closed=at(10))  # fix not deployed

    _, cfr, recovered = failures(repo.history())

    assert (cfr["failed"], cfr["deploys"]) == (3, 3)
    assert recovered["v1"].hours(v1.deployed_at) == 4 * 24  # v2 shipped #50, not the close
    assert recovered["v1"].via == "v2"
    assert recovered["v2"].hours(v2.deployed_at) == 10
    assert recovered["v2"].via == "issue #61 closed"
    assert recovered["v3"].recovered_at is None
    assert recovered["v3"].reason == "#51 not deployed yet"
    summary = recovery_time([v1, v2, v3], recovered)
    assert (summary["median_hours"], summary["open"]) == (10, 1)


@pytest.mark.parametrize(
    ("after", "window", "counted"),
    [
        (timedelta(hours=72), H72, True),
        (timedelta(hours=72, seconds=1), H72, False),
        (timedelta(hours=30), timedelta(hours=24), False),
        (timedelta(hours=30), timedelta(days=7), True),
    ],
)
def test_the_failure_window_is_inclusive_and_configurable(after, window, counted):
    repo = Repo()
    v1 = repo.deploy("v1", at(1))
    repo.issue(70, created=v1.deployed_at + after)

    (signal,) = failure_signals(repo.history(), window)

    assert signal.counted is counted
    assert signal.deploy_id == "v1"  # the nearest deploy is kept either way


def test_signals_before_any_deploy_or_on_excluded_items_never_count():
    repo = Repo()
    repo.issue(80, created=at(0))
    repo.deploy("v1", at(1))
    repo.pr(81, kinds=("hotfix",), merged=at(1, 5), excluded=True)
    repo.pr(82, kinds=("incident",), merged=None)  # closed without merging
    repo.issue(83, created=at(1, 2), kinds=("bug",))  # a bug is not a failure signal

    signals = failure_signals(repo.history(), H72)

    assert [(s.ref, s.outcome) for s in signals] == [("issue#80", "no_earlier_deploy")]


@pytest.mark.parametrize(
    ("title", "message", "expected"),
    [
        ("Revert #13052: Track HTTP response size", "", 13052),
        ("Revert PR-10073: Preserve scroll position", "", 10073),
        ('Revert "Make st.logout use end_session_endpoint (#11901)"', "", 11901),
        ('Revert "Add italic label"', "", 10243),  # GitHub's revert button: quoted title
        ("Revert accidental deprecation", "Revert x\n\nThis reverts commit abc1234.", 9999),
        ("Revert the layout changes", "", None),
    ],
)
def test_reverted_pr_reads_numbers_quoted_titles_and_commit_messages(title, message, expected):
    repo = Repo()
    for number, name in ((13052, "x"), (10073, "y"), (11901, "z"), (10243, "Add italic label")):
        repo.pr(number, title=name, merged=at(0))
    repo.pr(9999, title="Deprecate something", merged=at(0))
    repo.prs[9999] = replace(repo.prs[9999], merge_commit_sha="abc1234ff")
    revert = repo.pr(1, title=title, created=at(1), merged=at(1, 1))

    assert reverted_pr(revert, repo.history({"merge-1": message})) == expected


# --- bands ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.5, "elite"), (24, "elite"), (24.01, "high"), (168, "high"), (700, "medium"),
     (721, "low"), (None, None)],
)  # fmt: skip
def test_lower_is_better_bands_use_at_most_limits(value, expected):
    assert Band(24, 168, 720, "a report").of(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(7, "elite"), (6.9, "high"), (1, "high"), (0.5, "medium"), (0.2, "low")]
)
def test_deploy_frequency_bands_use_at_least_limits(value, expected):
    assert Band(7, 1, 0.23, "a report", higher_is_better=True).of(value) == expected


# --- the report -------------------------------------------------------------------------


def chain_history() -> History:
    repo = Repo()
    v1 = repo.deploy("v1", at(1))
    v2 = repo.deploy("v2", at(1, 20))
    v3 = repo.deploy("v3", at(1, 50))
    repo.deploy("v4", at(15))
    repo.pr(39, first_commit=at(0), merged=at(0, 12), shipped_in=v1)
    repo.pr(40, kinds=("hotfix",), created=at(1, 10), merged=at(1, 18), shipped_in=v2)
    repo.pr(41, kinds=("hotfix",), created=at(1, 25), merged=at(1, 45), shipped_in=v3)
    repo.pr(42, merged=at(20))
    repo.issue(43, created=at(20))  # 5 days after v4: outside the window
    return repo.history()


def test_the_report_traces_every_number_to_deploys_prs_and_signals(tmp_path):
    config = Config()  # no bands configured
    report = dora_report(chain_history(), [], [], repo="o/r", config=config, window=WINDOW)
    m = report["metrics"]

    assert (m["deployment_frequency"]["deploys"], m["deployment_frequency"]["band"]) == (4, None)
    assert m["lead_time"]["changes"] == 3
    assert m["lead_time"]["not_shipped"]["waiting_prs"] == [42]
    assert (m["change_failure_rate"]["failed"], m["change_failure_rate"]["deploys"]) == (2, 4)
    assert m["recovery_time"]["median_hours"] == 20
    assert [d["name"] for d in report["deploys"] if d["failed"]] == ["v1", "v2"]
    assert report["deploys"][1]["prs"] == [40]
    assert [f["signals"][0]["ref"] for f in report["failures"]] == ["pr#40", "pr#41"]
    assert [s["ref"] for s in report["ignored_signals"]] == ["issue#43"]

    rows = failure_rows(report)
    path = write_failures(rows, tmp_path / "failures.csv")
    with path.open() as handle:
        written = list(csv.DictReader(handle))
    assert [(r["deploy"], r["signal"], r["hours_after"], r["recovery_hours"]) for r in written] == [
        ("v1", "pr#40", "10.0", "20.0"),
        ("v2", "pr#41", "5.0", "30.0"),
    ]


def test_bands_come_from_config_with_their_source():
    bands = {
        "deployments_per_week": Band(7, 1, 0.23, "DORA 2024", higher_is_better=True),
        "change_failure_rate": Band(0.05, 0.10, 0.15, "DORA 2023"),
    }
    config = Config(dora=Dora(bands=bands))
    report = dora_report(chain_history(), [], [], repo="o/r", config=config, window=WINDOW)

    assert report["metrics"]["deployment_frequency"]["band"] == "high"  # 1.0 a week
    assert report["metrics"]["change_failure_rate"]["band"] == "low"  # 50%
    assert report["metrics"]["lead_time"]["band"] is None  # no limits configured
    assert report["bands"]["change_failure_rate"]["source"] == "DORA 2023"


def test_the_summary_reads_like_the_brief():
    config = Config(dora=Dora(bands={"change_failure_rate": Band(0.05, 0.1, 0.15, "DORA 2023")}))
    report = dora_report(chain_history(), [], [], repo="o/r", config=config, window=WINDOW)

    text = format_dora(report, "dora_failures_o__r.csv")
    lines = text.splitlines()

    assert lines[0] == "DORA (4 weeks, 2026-06-01 → 2026-06-28, deployment source: releases)"
    assert lines[1] == "Deployment frequency: 1.00/week (median gap 1.2d)"
    assert lines[3].startswith("Lead time for changes: p50 ")
    assert "Change failure rate: 50% (2 of 4 deploys; see dora_failures_o__r.csv)" in text
    assert "band: low" in text
    assert "Failed deployment recovery time: median 20.0h (2 recovered)" in text
    assert "  v1 (2026-06-02), recovered in 20.0h: v2, shipped #40" in text
    assert "Signals in the window that didn't count: 1 outside the failure window" in text


def test_each_deploy_says_how_much_of_what_it_shipped_is_known():
    repo = Repo()
    for name, day in (("v1", 1), ("v2", 2), ("v3", 3), ("v4", 4), ("v5", 5)):
        repo.deploy(name, at(day))
    repo.deploys.append(Deploy("v6", "releases", "v6", "sha-v5", None, "success", at(6), None))
    repo.pr(1, merged=at(1, 12))
    compares = [
        {"id": "sha-v1...sha-v2", "status": "ahead", "total_commits": 2, "fetched": 2},
        {"id": "sha-v2...sha-v3", "status": "diverged", "total_commits": 300, "fetched": 250},
        {"id": "sha-v3...sha-v4", "status": "not_found", "total_commits": None, "fetched": 0},
    ]
    commits = [
        {"compare_id": "sha-v1...sha-v2", "position": 0, "sha": "merge-1", "message": "One (#1)"},
        {"compare_id": "sha-v1...sha-v2", "position": 1, "sha": "bump", "message": "Up version"},
    ]

    report = dora_report(
        repo.history(), compares, commits, repo="o/r", config=Config(), window=WINDOW
    )

    assert [(d["name"], d["range"]["status"]) for d in report["deploys"]] == [
        ("v1", "first deploy: nothing to compare with"),
        ("v2", "complete"),
        ("v3", "partial: 250 of 300 commits"),
        ("v4", "GitHub couldn't compare the commits"),
        ("v5", "not fetched: run flowlens ingest"),
        ("v6", "same commit as the previous deploy"),
    ]
    assert (
        report["deploys"][1]["range"]["commits"],
        report["deploys"][1]["range"]["commits_without_pr"],
    ) == (2, 1)
