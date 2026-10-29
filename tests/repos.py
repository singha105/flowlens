"""Repos for the dashboard and report tests: a synthetic team with a known DORA history,
and the recorded streamlit fixtures loaded into a database the way an ingest leaves it."""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from fakes import fill_with_streamlit
from flowlens.config import Config, load_config
from flowlens.metrics.dora import History
from flowlens.model.build import build_model
from flowlens.snapshot import RepoData
from flowlens.storage import Store, db_path_for
from synthetic import Scenario, generate
from test_dora import chain_history

ROOT = Path(__file__).parent.parent
STREAMLIT = "streamlit/streamlit"


def synthetic_repo(config: Config | None = None, *, days: int = 182, seed: int = 3) -> RepoData:
    """Six months of a simulated team (2026-01-05 to 07-06) and four June deploys, two of
    them failed (tests/test_dora.py chain_history)."""
    team = generate(seed=seed, scenario=Scenario(days=days))
    return RepoData(
        repo="acme/widgets",
        config=config or Config(),
        items=team.frame(),
        prs=team.pr_frame(),
        as_of=pd.Timestamp(team.as_of),
        history_from=None,
        history=chain_history(),
        compares=[],
        commits=[],
    )


def empty_repo() -> RepoData:
    team = generate(seed=0, scenario=Scenario(days=28))
    return RepoData(
        repo="acme/empty",
        config=Config(),
        items=team.frame().iloc[0:0],
        prs=team.pr_frame().iloc[0:0],
        as_of=pd.Timestamp(team.as_of),
        history_from=None,
        history=History([], {}, {}, [], {}),
        compares=[],
        commits=[],
    )


def streamlit_db(data_dir: Path) -> Path:
    """The recorded streamlit fixtures as one successful ingest, with the model built."""
    path = db_path_for(data_dir, "streamlit", "streamlit")
    with Store(path) as store:
        fill_with_streamlit(
            store,
            coverage_from=datetime(2025, 10, 29, tzinfo=UTC),
            finished_at=datetime(2026, 10, 29, 5, tzinfo=UTC),
        )
        build_model(store, load_config(ROOT / "flowlens.yaml", STREAMLIT), STREAMLIT)
    return path
