"""Everything the dashboard and the HTML report show for one repo, loaded once.

`load_repo` reads the model tables into a `RepoData`; its methods compute each view for a
window with the same functions as ``flowlens flow``, ``forecast``, ``backtest``, and
``dora``, so the dashboard, the report, and the command line agree on every number.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from flowlens.config import Config
from flowlens.metrics import flow
from flowlens.metrics.dora import History
from flowlens.metrics.dora_report import dora_report, load_history
from flowlens.metrics.flow import WEEK, Window
from flowlens.metrics.forecast_report import Throughput, throughput_from
from flowlens.metrics.report import flow_report
from flowlens.storage import Store

DB_SUFFIX = ".duckdb"


@dataclass(frozen=True)
class RepoData:
    repo: str
    config: Config
    items: pd.DataFrame
    prs: pd.DataFrame
    as_of: pd.Timestamp  # the last successful ingest
    history_from: datetime | None  # how far back that ingest reaches
    history: History
    compares: list[dict[str, Any]]
    commits: list[dict[str, Any]]

    @property
    def latest_end(self) -> pd.Timestamp:
        """The end of the last whole week before as_of."""
        return Window.ending(self.as_of, 1).end

    @property
    def first_week(self) -> pd.Timestamp:
        """The first Monday the ingested history fully covers."""
        floor = pd.Timestamp(self.history_from or self.items["created_at"].min()).normalize()
        return floor + pd.Timedelta(days=(7 - floor.weekday()) % 7)

    def window(self, weeks: int = 26) -> Window:
        return Window.ending(self.as_of, weeks)

    def window_between(self, first_day: date, last_day: date) -> Window:
        """Whole weeks from the Monday on or before ``first_day`` through the week holding
        ``last_day``, cut at the last whole week. WIP and aging are as of the window's end,
        or the last ingest when the window reaches it."""
        start = _monday(first_day)
        end = min(_monday(last_day) + WEEK, self.latest_end)
        start = min(start, end - WEEK)
        as_of = self.as_of if end == self.latest_end else end
        return Window(start=start, end=end, as_of=as_of)

    def flow(self, window: Window) -> dict[str, Any]:
        return flow_report(
            self.items, self.prs, repo=self.repo, window=window, history_from=self.history_from
        )

    def finished(self, window: Window) -> pd.DataFrame:
        """Items delivered in the window with lead and cycle time, finish date, and title."""
        durations = flow.durations(self.items, window)
        details = self.items.set_index("id")[["title", "url", "started_at", "done_at"]]
        return durations.join(details, on="id")

    def dora(self, window: Window) -> dict[str, Any]:
        return dora_report(
            self.history,
            self.compares,
            self.commits,
            repo=self.repo,
            config=self.config,
            window=window,
        )

    def throughput(self, unit: str) -> Throughput:
        return throughput_from(self.items, self.repo, unit, self.as_of, self.history_from)


def load_repo(store: Store, config: Config, repo: str) -> RepoData:
    """The model as the last ``flowlens`` command built it."""
    run = store.last_successful_run(repo)
    as_of = pd.Timestamp((run.finished_at if run else None) or datetime.now(UTC))
    history, compares, commits = load_history(store)
    return RepoData(
        repo=repo,
        config=config,
        items=flow.work_item_frame(store.rows("work_items")),
        prs=flow.pull_request_frame(store.rows("pull_requests")),
        as_of=as_of,
        history_from=run.coverage_from if run else None,
        history=history,
        compares=compares,
        commits=commits,
    )


def repos_in(data_dir: Path) -> list[str]:
    """``owner/name`` for every database in ``data_dir``, alphabetically."""
    names = sorted(path.name.removesuffix(DB_SUFFIX) for path in data_dir.glob(f"*{DB_SUFFIX}"))
    return [name.replace("__", "/", 1) for name in names if "__" in name]


def _monday(day: date | datetime | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(day)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    midnight = stamp.normalize()
    return midnight - pd.Timedelta(days=midnight.weekday())
