"""Data-quality report: how much of a repo's history FlowLens can actually measure.

Low coverage is a finding in its own right: if most issues close without ever being
assigned or linked to a PR, issue cycle time describes a minority of the work, and the
report should say so before any chart does.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from flowlens.config import Config
from flowlens.storage import Store

# Below these shares, the report flags the gap as a finding.
START_COVERAGE_FLOOR = 0.6
REVIEW_COVERAGE_FLOOR = 0.5
NO_REASON_CEILING = 0.05
BOT_SHARE_CEILING = 0.3


@dataclass(frozen=True)
class QualityReport:
    repo: str
    window_start: datetime | None
    window_end: datetime
    deployment_source: str
    work_items: int
    issues: int
    prs: int
    excluded: int
    excluded_by: dict[str, int]
    included: int
    included_started: int
    included_issues: int
    issues_started: int
    issue_signals: dict[str, int]
    issues_done: int
    issues_done_without_start: int
    issues_closed: int
    issues_closed_no_reason: int
    included_prs: int
    prs_merged: int
    prs_merged_reviewed: int
    prs_merged_approved: int
    median_hours_to_first_review: float | None
    deploys: int
    deploy_failures: int
    releases: int
    deployments_by_environment: dict[str, int]
    workflow_runs: dict[str, int]

    @property
    def months(self) -> float | None:
        if self.window_start is None:
            return None
        return (self.window_end - self.window_start).days / 30.44

    @property
    def findings(self) -> list[str]:
        """Plain-language warnings about coverage gaps that weaken a metric."""
        found = []
        if self.included_issues and _share(self.issues_started, self.included_issues) < (
            START_COVERAGE_FLOOR
        ):
            found.append(
                f"Only {_pct(self.issues_started, self.included_issues)} of issues have a start "
                "signal (assignment, linked PR, or start label). Issue cycle time will describe "
                "a minority of the work; lead time (opened to closed) covers all of it."
            )
        if self.issues_done and self.issues_done_without_start:
            found.append(
                f"{self.issues_done_without_start:,} of {self.issues_done:,} finished issues "
                f"({_pct(self.issues_done_without_start, self.issues_done)}) closed without ever "
                "starting: no assignment, linked PR, or start label before they closed."
            )
        if self.issues_closed and _share(self.issues_closed_no_reason, self.issues_closed) > (
            NO_REASON_CEILING
        ):
            share = _pct(self.issues_closed_no_reason, self.issues_closed)
            found.append(
                f"{share} of closed issues have no closing reason, so completed and abandoned "
                "work can't be told apart."
            )
        if self.prs_merged and _share(self.prs_merged_reviewed, self.prs_merged) < (
            REVIEW_COVERAGE_FLOOR
        ):
            found.append(
                f"Only {_pct(self.prs_merged_reviewed, self.prs_merged)} of merged PRs had a human "
                "review, so time to first review describes a minority of PRs."
            )
        bots = self.excluded_by.get("bot", 0)
        if self.prs and _share(bots, self.work_items) > BOT_SHARE_CEILING:
            found.append(
                f"{_pct(bots, self.work_items)} of work items are from bots and are excluded; "
                "throughput would look very different with them in."
            )
        if self.deploys == 0:
            alternatives = []
            if self.releases:
                alternatives.append(f"releases ({self.releases})")
            for environment, count in self.deployments_by_environment.items():
                alternatives.append(f"deployments to {environment} ({count})")
            for workflow, count in self.workflow_runs.items():
                alternatives.append(f"workflow:{workflow} ({count} runs)")
            hint = f" Signals found: {', '.join(alternatives)}." if alternatives else ""
            found.append(
                f"No deploys found from {self.deployment_source}; DORA metrics need one.{hint}"
            )
        elif self.months and self.deploys / self.months < 1:
            found.append(
                f"Fewer than one deploy a month from {self.deployment_source}; deployment "
                "frequency and change failure rate will rest on very few points."
            )
        return found


def build_report(store: Store, config: Config, repo: str) -> QualityReport:
    """Measure coverage over the model tables (build the model first)."""
    con = store.con
    run = store.last_successful_run(repo)
    window_end = (run.finished_at if run and run.finished_at else None) or datetime.now(UTC)
    window_start = run.coverage_from if run else None
    if window_start is None:
        (earliest,) = con.execute("SELECT min(created_at) FROM work_items").fetchone()
        window_start = earliest.replace(tzinfo=UTC) if earliest else None
    start = window_start.replace(tzinfo=None) if window_start else datetime(1970, 1, 1)

    totals = dict(con.execute("SELECT type, count(*) FROM work_items GROUP BY type").fetchall())
    excluded_by = dict(
        con.execute(
            "SELECT exclude_reason, count(*) FROM work_items WHERE excluded "
            "GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
    )

    included, included_started = con.execute(
        "SELECT count(*), count(started_at) FROM work_items WHERE NOT excluded"
    ).fetchone()
    issues = con.execute(
        """
        SELECT count(*), count(started_at),
               count(*) FILTER (WHERE done_at IS NOT NULL),
               count(*) FILTER (WHERE done_at IS NOT NULL AND started_at IS NULL)
        FROM work_items WHERE type = 'issue' AND NOT excluded
        """
    ).fetchone()
    issue_signals = dict(
        con.execute(
            """
            SELECT started_signal, count(*) FROM work_items
            WHERE type = 'issue' AND NOT excluded AND started_signal IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC, 1
            """
        ).fetchall()
    )
    closed, closed_no_reason = con.execute(
        """
        SELECT count(*) FILTER (WHERE outcome <> 'open'),
               count(*) FILTER (WHERE outcome = 'closed_no_reason')
        FROM work_items WHERE type = 'issue'
        """
    ).fetchone()
    prs = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE w.outcome = 'merged'),
               count(*) FILTER (WHERE w.outcome = 'merged' AND p.review_count > 0),
               count(*) FILTER (WHERE w.outcome = 'merged' AND p.first_approval_at IS NOT NULL),
               median(epoch(p.first_review_at - p.created_at) / 3600)
                   FILTER (WHERE w.outcome = 'merged' AND p.first_review_at IS NOT NULL)
        FROM work_items w JOIN pull_requests p ON w.type = 'pr' AND p.number = w.number
        WHERE NOT w.excluded
        """
    ).fetchone()
    deploys, deploy_failures = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE status = 'failure') FROM deploys "
        "WHERE deployed_at >= ?",
        [start],
    ).fetchone()
    (releases,) = con.execute(
        "SELECT count(*) FROM releases WHERE NOT is_draft AND published_at >= ?", [start]
    ).fetchone()
    deployments_by_environment = dict(
        con.execute(
            "SELECT coalesce(environment, '(none)'), count(*) FROM deployments "
            "WHERE created_at >= ? GROUP BY 1 ORDER BY 2 DESC, 1",
            [start],
        ).fetchall()
    )
    workflow_runs = dict(
        con.execute(
            "SELECT workflow_name, count(*) FROM workflow_runs WHERE created_at >= ? "
            "GROUP BY 1 ORDER BY 2 DESC, 1",
            [start],
        ).fetchall()
    )

    return QualityReport(
        repo=repo,
        window_start=window_start,
        window_end=window_end,
        deployment_source=config.deployment_source,
        work_items=sum(totals.values()),
        issues=totals.get("issue", 0),
        prs=totals.get("pr", 0),
        excluded=sum(excluded_by.values()),
        excluded_by=excluded_by,
        included=included,
        included_started=included_started,
        included_issues=issues[0],
        issues_started=issues[1],
        issue_signals=issue_signals,
        issues_done=issues[2],
        issues_done_without_start=issues[3],
        issues_closed=closed,
        issues_closed_no_reason=closed_no_reason,
        included_prs=prs[0],
        prs_merged=prs[1],
        prs_merged_reviewed=prs[2],
        prs_merged_approved=prs[3],
        median_hours_to_first_review=prs[4],
        deploys=deploys,
        deploy_failures=deploy_failures,
        releases=releases,
        deployments_by_environment=deployments_by_environment,
        workflow_runs=workflow_runs,
    )


def format_report(report: QualityReport) -> str:
    r = report
    months = r.months
    window = "no data"
    if r.window_start is not None:
        window = f"{r.window_start:%Y-%m-%d} → {r.window_end:%Y-%m-%d} ({months:.1f} months)"
    not_planned = r.excluded_by.get("state_reason:NOT_PLANNED", 0)
    signals = f" ({_signals(r.issue_signals, r.included_issues)})" if r.issue_signals else ""
    never_started = (
        f"{r.issues_done_without_start:,} of {r.issues_done:,} finished "
        f"({_pct(r.issues_done_without_start, r.issues_done)})"
    )
    no_reason = (
        f"{r.issues_closed_no_reason:,} of {r.issues_closed:,} closed "
        f"({_pct(r.issues_closed_no_reason, r.issues_closed)})"
    )
    deploys = f"{r.deploys:,}"
    if months:
        deploys += f" in {months:.0f} months"
        if r.deploys:
            deploys += f" ({r.deploys / months:.1f} a month)"
    if r.deploy_failures:
        deploys += f", {r.deploy_failures:,} failed"

    lines = [
        f"Data quality · {r.repo} · {window}",
        "",
        f"Work items: {r.work_items:,} | with started_at: {_pct(r.included_started, r.included)}"
        f" | closed not planned (excluded): {_pct(not_planned, r.work_items)}",
        f"  {r.issues:,} issues, {r.prs:,} PRs; {r.excluded:,} excluded "
        f"({_reasons(r.excluded_by)})",
        f"Issues: {r.included_issues:,} counted | with started_at: "
        f"{_pct(r.issues_started, r.included_issues)}{signals}",
        f"  closed without ever starting: {never_started} | no closing reason: {no_reason}",
        f"PRs: {r.included_prs:,} counted, {r.prs_merged:,} merged | with a review: "
        f"{_pct(r.prs_merged_reviewed, r.prs_merged)} | median time to first review: "
        f"{_duration(r.median_hours_to_first_review)}",
        f"  approved before merge: {_pct(r.prs_merged_approved, r.prs_merged)}"
        f" | merged with no human review: {r.prs_merged - r.prs_merged_reviewed:,}",
        f"Deployments ({r.deployment_source}): {deploys}",
        f"  signals found: {_sources(r)}",
    ]
    findings = r.findings
    lines += ["", "Findings:" if findings else "Findings: none, coverage looks healthy."]
    lines += [f"  ! {finding}" for finding in findings]
    return "\n".join(lines)


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _pct(part: int, whole: int) -> str:
    return f"{_share(part, whole):.0%}" if whole else "n/a"


def _duration(hours: float | None) -> str:
    if hours is None:
        return "n/a"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def _reasons(excluded_by: dict[str, int]) -> str:
    labels = {
        "bot": "bot authors",
        "state_reason:NOT_PLANNED": "not planned",
        "state_reason:DUPLICATE": "duplicates",
    }
    parts = sorted(excluded_by.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{labels.get(reason, reason)} {count:,}" for reason, count in parts) or "none"


def _signals(signals: dict[str, int], total: int) -> str:
    return ", ".join(f"{signal} {_pct(count, total)}" for signal, count in signals.items())


def _sources(report: QualityReport) -> str:
    parts = [f"releases {report.releases:,}"]
    if report.deployments_by_environment:
        environments = ", ".join(
            f"{env} {count:,}" for env, count in report.deployments_by_environment.items()
        )
        parts.append(
            f"deployments {sum(report.deployments_by_environment.values()):,} ({environments})"
        )
    else:
        parts.append("deployments 0")
    for workflow, count in report.workflow_runs.items():
        parts.append(f"workflow {workflow!r} {count:,} runs")
    return " · ".join(parts)
