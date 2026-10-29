"""``flowlens dora`` for one repo: load the history, compute the four keys, write, print.

`dora_report` turns flowlens.metrics.dora into one JSON-ready dict that keeps every deploy,
shipped PR, and failure signal behind the numbers (data/out/dora_<owner>__<name>.json).
`failure_rows` lists each failure signal that counted, for
data/out/dora_failures_<owner>__<name>.csv. `format_dora` prints the summary.
"""

import csv
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from flowlens.config import Config
from flowlens.metrics.dora import (
    Change,
    History,
    Issue,
    PullRequest,
    Signal,
    change_failure_rate,
    counted_by_deploy,
    deployment_frequency,
    failure_signals,
    in_window,
    lead_time,
    not_shipped,
    recoveries,
    recovery_time,
    shipped_changes,
)
from flowlens.metrics.flow import Window
from flowlens.model.changes import DeploymentChange, deploy_ranges, referenced_pr
from flowlens.model.deploys import Deploy
from flowlens.storage import Store

# Report section -> its key under dora.bands in flowlens.yaml, and the value it's banded on.
BANDED = {
    "deployment_frequency": ("deployments_per_week", lambda m: m["per_week"]),
    "lead_time": ("lead_time_hours", lambda m: _hours(m["p50_days"])),
    "change_failure_rate": ("change_failure_rate", lambda m: m["rate"]),
    "recovery_time": ("recovery_hours", lambda m: m["median_hours"]),
}
BAND_COLUMN = 64
OUTCOMES = {
    "outside_window": "outside the failure window",
    "no_earlier_deploy": "before the first deploy",
    "never_shipped": "reverting a PR no deploy shipped",
    "before_shipping": "reverting a PR before it shipped",
}
FAILURE_COLUMNS = (
    "deploy", "deployed_at", "kind", "signal", "title", "url", "signal_at", "hours_after",
    "reason", "recovered_at", "recovery_hours", "recovered_via",
)  # fmt: skip


def load_history(store: Store) -> tuple[History, list[dict[str, Any]], list[dict[str, Any]]]:
    """The model's deploys and changes with the PRs, issues, and compares behind them."""
    items = {item["id"]: item for item in store.rows("work_items")}
    prs = {}
    for row in store.rows("pull_requests"):
        item = items.get(f"pr#{row['number']}", {})
        prs[row["number"]] = PullRequest(
            number=row["number"],
            title=row["title"],
            url=row["url"],
            created_at=row["created_at"],
            merged_at=row["merged_at"],
            first_commit_at=row["first_commit_at"],
            merge_commit_sha=row["merge_commit_sha"],
            base_ref_name=row["base_ref_name"],
            kinds=tuple(item.get("kinds") or ()),
            excluded=bool(item.get("excluded")),
        )
    issues = []
    for row in store.rows("issues"):
        item = items.get(f"issue#{row['number']}", {})
        issues.append(
            Issue(
                number=row["number"],
                title=row["title"],
                url=row["url"],
                created_at=row["created_at"],
                closed_at=row["closed_at"] if row["state"] == "CLOSED" else None,
                kinds=tuple(item.get("kinds") or ()),
                linked=tuple(item.get("linked") or ()),
                excluded=bool(item.get("excluded")),
            )
        )
    commits = store.rows("compare_commits")
    history = History(
        deploys=[Deploy(**row) for row in store.rows("deploys")],
        changes={
            row["pr_number"]: DeploymentChange(**row) for row in store.rows("deployment_changes")
        },
        pull_requests=prs,
        issues=issues,
        messages={commit["sha"]: commit["message"] for commit in commits},
    )
    return history, store.rows("compares"), commits


def dora_report_for(store: Store, config: Config, repo: str, *, weeks: int) -> dict[str, Any]:
    """The report as of the last successful ingest (or now, if there wasn't one)."""
    run = store.last_successful_run(repo)
    as_of = (run.finished_at if run else None) or datetime.now(UTC)
    history, compares, commits = load_history(store)
    return dora_report(
        history,
        compares,
        commits,
        repo=repo,
        config=config,
        window=Window.ending(as_of, weeks),
    )


def dora_report(
    history: History,
    compares: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    *,
    repo: str,
    config: Config,
    window: Window,
) -> dict[str, Any]:
    failure_window = timedelta(hours=config.dora.failure_window_hours)
    signals = failure_signals(history, failure_window)
    recovered = recoveries(history, signals)
    changes = shipped_changes(history, window)
    frequency = deployment_frequency(history.deploys, window)
    failures = change_failure_rate(history.deploys, signals, window)
    failed = failures.pop("failed_deploys")
    waiting = not_shipped(history, window)
    window_deploys = in_window(history.deploys, window)
    as_of = window.as_of.to_pydatetime()
    still_open = [d for d in window_deploys if as_of - d.deployed_at < failure_window]
    window_ids = {d.id for d in window_deploys}
    left_out = sum(
        1
        for number, change in history.changes.items()
        if change.deploy_id in window_ids
        and number in history.pull_requests
        and history.pull_requests[number].excluded
    )

    metrics = {
        "deployment_frequency": frequency,
        "lead_time": {
            **lead_time(changes),
            "excluded_shipped": left_out,  # bot PRs and other excluded items
            "not_shipped": {
                "main_line": waiting["main_line"],
                "waiting": len(waiting["waiting"]),
                "other_branches": len(waiting["other_branches"]),
                "waiting_prs": [pr.number for pr in waiting["waiting"]],
            },
        },
        "change_failure_rate": {
            **failures,
            "failure_window_hours": config.dora.failure_window_hours,
            "inside_failure_window": [_name(d) for d in still_open],
        },
        "recovery_time": recovery_time(failed, recovered),
    }
    bands = {}
    for section, (key, value) in BANDED.items():
        band = config.dora.bands.get(key)
        metrics[section]["band"] = band.of(value(metrics[section])) if band else None
        if band:
            bands[key] = {
                "elite": band.elite,
                "high": band.high,
                "medium": band.medium,
                "higher_is_better": band.higher_is_better,
                "source": band.source,
            }

    by_deploy = counted_by_deploy(signals)
    ranges = _ranges(history, compares, commits)
    shipped_by_deploy: dict[str, list[int]] = {}
    for change in changes:
        shipped_by_deploy.setdefault(change.deploy.id, []).append(change.pr.number)
    ignored = [s for s in signals if not s.counted and window.start <= s.at <= as_of]
    return {
        "repo": repo,
        "as_of": _iso(window.as_of),
        "window": {"start": _iso(window.start), "end": _iso(window.end), "weeks": window.weeks},
        "deployment_source": config.deployment_source,
        "metrics": _jsonable(metrics),
        "bands": bands,
        "deploys": [
            {
                **_deploy(deploy),
                "range": ranges.get(deploy.id),
                "prs": shipped_by_deploy.get(deploy.id, []),
                "failed": deploy.id in by_deploy,
                "signals": [s.ref for s in by_deploy.get(deploy.id, [])],
            }
            for deploy in window_deploys
        ],
        "failures": [
            {
                **_deploy(deploy),
                "signals": [_signal(s, history) for s in by_deploy[deploy.id]],
                "recovered_at": _iso(recovered[deploy.id].recovered_at),
                "recovery_hours": recovered[deploy.id].hours(deploy.deployed_at),
                "recovered_via": recovered[deploy.id].via,
                "recovery_note": recovered[deploy.id].reason,
            }
            for deploy in failed
        ],
        "changes": [_change(change) for change in changes],
        "ignored_signals": [_signal(s, history) for s in ignored],
    }


def _ranges(
    history: History, compares: list[dict[str, Any]], commits: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """What's known about the commits each deploy shipped, by deploy id."""
    stored = {row["id"]: row for row in compares}
    counts: dict[str, list[int]] = {}  # compare id -> [commits, commits naming no PR]
    merge_commits = {pr.merge_commit_sha for pr in history.pull_requests.values() if pr.merged_at}
    for commit in commits:
        tally = counts.setdefault(commit["compare_id"], [0, 0])
        tally[0] += 1
        named = referenced_pr(commit["message"])
        if commit["sha"] not in merge_commits and named not in history.pull_requests:
            tally[1] += 1
    ranges = {}
    for deploy_range in deploy_ranges(history.deploys):
        range_id = deploy_range.compare_id
        row = stored.get(range_id or "")
        if deploy_range.base_sha is None:
            status = "first deploy: nothing to compare with"
        elif deploy_range.head_sha is None:
            status = "no commit"
        elif range_id is None:
            status = "same commit as the previous deploy"
        elif row is None:
            status = "not fetched: run flowlens ingest"
        elif row["status"] == "not_found":
            status = "GitHub couldn't compare the commits"
        elif row["fetched"] < (row["total_commits"] or 0):
            status = f"partial: {row['fetched']} of {row['total_commits']} commits"
        else:
            status = "complete"
        total, other = counts.get(range_id or "", [0, 0])
        ranges[deploy_range.deploy_id] = {
            "base_sha": deploy_range.base_sha,
            "status": status,
            "commits": total,
            "commits_without_pr": other,
        }
    return ranges


def failure_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per failure signal that counted against a deploy in the window."""
    rows = []
    for failure in report["failures"]:
        for signal in failure["signals"]:
            rows.append(
                {
                    "deploy": failure["name"],
                    "deployed_at": failure["deployed_at"],
                    "kind": signal["kind"],
                    "signal": signal["ref"],
                    "title": signal["title"],
                    "url": signal["url"],
                    "signal_at": signal["at"],
                    "hours_after": signal["hours_after"],
                    "reason": signal["reason"],
                    "recovered_at": failure["recovered_at"],
                    "recovery_hours": failure["recovery_hours"],
                    "recovered_via": _via(failure),
                }
            )
    return rows


def write_failures(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FAILURE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: f"{value:.1f}" if isinstance(value, float) else value
                    for key, value in row.items()
                }
            )
    return path


def format_dora(report: dict[str, Any], failures_file: str | None = None) -> str:
    r = report
    m = r["metrics"]
    weeks = r["window"]["weeks"]
    last_day = (pd.Timestamp(r["window"]["end"]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    lines = [
        f"DORA ({weeks} weeks, {r['window']['start'][:10]} → {last_day}, "
        f"deployment source: {r['deployment_source']})"
    ]

    def metric(text: str, section: str) -> None:
        band = m[section]["band"]
        lines.append(f"{text.ljust(BAND_COLUMN)}  band: {band}" if band else text)

    freq = m["deployment_frequency"]
    gap = _days(freq["median_gap_days"])
    metric(
        f"Deployment frequency: {freq['per_week']:.2f}/week (median gap {gap})",
        "deployment_frequency",
    )
    lines.append(
        f"  {_plural(freq['deploys'], 'deploy')}, {freq['per_day']:.2f}/day; "
        f"{freq['weeks_without']} of {weeks} weeks had none"
    )

    lead = m["lead_time"]
    if lead["changes"]:
        p50, p85 = _days(lead["p50_days"]), _days(lead["p85_days"])
        metric(f"Lead time for changes: p50 {p50}, p85 {p85}", "lead_time")
        excluded = (
            f" (plus {lead['excluded_shipped']:,} excluded, such as bots)"
            if lead["excluded_shipped"]
            else ""
        )
        lines.append(
            f"  {_plural(lead['changes'], 'PR')}{excluded}: "
            f"coding p50 {_days(lead['coding_p50_days'])}, "
            f"waiting to deploy p50 {_days(lead['waiting_p50_days'])}; waiting is "
            f"{lead['waiting_share']:.0%} of all lead time"
        )
    else:
        metric("Lead time for changes: no PRs shipped in the window", "lead_time")
    pending = lead["not_shipped"]
    if pending["waiting"] or pending["other_branches"]:
        lines.append(
            f"  not deployed: {_plural(pending['waiting'], 'PR')} merged to "
            f"{pending['main_line'] or 'the main line'} in the window, "
            f"{pending['other_branches']} into other branches"
        )

    cfr = m["change_failure_rate"]
    if cfr["deploys"]:
        see = f"; see {failures_file}" if failures_file and cfr["failed"] else ""
        metric(
            f"Change failure rate: {cfr['rate']:.0%} ({cfr['failed']} of "
            f"{_plural(cfr['deploys'], 'deploy')}{see})",
            "change_failure_rate",
        )
    else:
        metric("Change failure rate: no deploys in the window", "change_failure_rate")

    rec = m["recovery_time"]
    if rec["recovered"]:
        still = f", {rec['open']} not yet" if rec["open"] else ""
        metric(
            f"Failed deployment recovery time: median {_hours_text(rec['median_hours'])} "
            f"({rec['recovered']} recovered{still})",
            "recovery_time",
        )
    elif rec["failed"]:
        text = f"Failed deployment recovery time: none of {rec['failed']} recovered yet"
        metric(text, "recovery_time")
    else:
        metric("Failed deployment recovery time: no failed deploys", "recovery_time")

    if r["failures"]:
        lines += ["", "Failed deploys:"]
        for failure in r["failures"]:
            if failure["recovery_hours"] is not None:
                hours = _hours_text(failure["recovery_hours"])
                end = (
                    f"recovered in {hours}: {failure['recovered_via']}, {failure['recovery_note']}"
                )
            else:
                end = f"not recovered: {failure['recovery_note']}"
            lines.append(f"  {failure['name']} ({failure['deployed_at'][:10]}), {end}")
            lines += [
                f"    {_ref(s['ref'])} {_clip(s['title'])}: {s['reason']}"
                for s in failure["signals"]
            ]
    ignored = r["ignored_signals"]
    if ignored:
        why = Counter(s["outcome"] for s in ignored)
        summary = ", ".join(f"{n} {OUTCOMES[outcome]}" for outcome, n in why.most_common())
        lines += ["", f"Signals in the window that didn't count: {summary}"]
        lines += [f"  {_ref(s['ref'])} {_clip(s['title'])}: {s['reason']}" for s in ignored[-5:]]
        if len(ignored) > 5:
            lines.append(f"  and {len(ignored) - 5} earlier, listed in the JSON")
    if cfr["inside_failure_window"]:
        names = ", ".join(cfr["inside_failure_window"])
        hours = cfr["failure_window_hours"]
        lines.append(f"\nStill inside the {hours:.0f}h failure window, so may yet fail: {names}")
    incomplete = [d for d in r["deploys"] if d["range"] and d["range"]["status"] != "complete"]
    if incomplete:
        lines.append(
            "\nDeploys whose shipped PRs aren't fully known: "
            + "; ".join(f"{d['name']} ({d['range']['status']})" for d in incomplete)
        )
    if r["bands"]:
        sources: dict[str, list[str]] = {}
        for key, band in r["bands"].items():
            sources.setdefault(band["source"], []).append(key)
        lines.append("\nBands are approximate; limits and sources are in flowlens.yaml:")
        lines += [f"  {', '.join(keys)}: {source}" for source, keys in sources.items()]
    else:
        lines.append("\nNo bands: add dora.bands to flowlens.yaml to rate each metric.")
    return "\n".join(lines)


def _signal(signal: Signal, history: History) -> dict[str, Any]:
    deploy = history.deploy(signal.deploy_id) if signal.deploy_id else None
    return {
        "kind": signal.kind,
        "ref": signal.ref,
        "title": signal.title,
        "url": signal.url,
        "at": _iso(signal.at),
        "deploy": _name(deploy) if deploy else None,
        "hours_after": (signal.at - deploy.deployed_at) / timedelta(hours=1) if deploy else None,
        "outcome": signal.outcome,
        "reason": signal.reason,
        "fix_prs": list(signal.fix_prs),
    }


def _change(change: Change) -> dict[str, Any]:
    day = timedelta(days=1)
    return {
        "pr": change.pr.number,
        "title": change.pr.title,
        "url": change.pr.url,
        "deploy": _name(change.deploy),
        "first_commit_at": _iso(change.pr.first_commit_at),
        "merged_at": _iso(change.pr.merged_at),
        "deployed_at": _iso(change.deploy.deployed_at),
        "lead_days": change.lead / day,
        "coding_days": change.coding / day,
        "waiting_days": change.waiting / day,
        "matched_by": change.matched_by,
    }


def _deploy(deploy: Deploy) -> dict[str, Any]:
    return {
        "id": deploy.id,
        "name": _name(deploy),
        "sha": deploy.sha,
        "url": deploy.url,
        "status": deploy.status,
        "deployed_at": _iso(deploy.deployed_at),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _iso(value: Any) -> str | None:
    if value is None or value is pd.NaT:
        return None
    return pd.Timestamp(value).tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _name(deploy: Deploy) -> str:
    return deploy.name or deploy.id


def _hours(days: float | None) -> float | None:
    return None if days is None else days * 24


def _days(days: float | None) -> str:
    return "-" if days is None else f"{days:.1f}d"


def _hours_text(hours: float | None) -> str:
    if hours is None:
        return "-"
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def _ref(ref: str) -> str:
    kind, _, number = ref.partition("#")
    return {"pr": "PR", "issue": "issue"}.get(kind, ref) + (f" #{number}" if number else "")


def _via(failure: dict[str, Any]) -> str:
    via, note = failure["recovered_via"], failure["recovery_note"]
    return f"{via} ({note})" if via and note else via or note


def _clip(title: str, width: int = 60) -> str:
    return repr(title if len(title) <= width else title[: width - 1].rstrip() + "…")


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"
