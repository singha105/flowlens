# FlowLens

FlowLens turns a GitHub repository's history into the numbers delivery conversations actually need: cycle time, throughput, and aging work in progress; Monte Carlo forecasts that answer "when will these 30 items be done?" with a date range and a confidence level; and DORA metrics built from real merges, releases, and deployments. It runs locally on your own `gh` credentials, stores everything in a DuckDB file, writes every metric definition down, and costs nothing to run.

> Status: early development (Day 1 of 7). Ingestion of issues and pull requests works; metrics, forecasts, and the dashboard are on the way. See [docs/PRD.md](docs/PRD.md) and [docs/PROGRESS.md](docs/PROGRESS.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and the [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`.

```bash
git clone https://github.com/singha105/flowlens && cd flowlens
uv run flowlens ingest --repo pallets/markupsafe --since 2025-10-01
```

This writes `data/pallets__markupsafe.duckdb` with `issues`, `pull_requests`, and `ingest_runs` tables. Run the same command again and only items updated since the last run are fetched. Pass `--full` to refetch everything from `--since`.

The token is read from `gh auth token` each time FlowLens runs and is never written to disk.

## Layout

```
flowlens/
├── docs/
│   ├── PRD.md              product requirements
│   └── PROGRESS.md         daily build log
├── src/flowlens/
│   ├── cli.py              `flowlens` command (typer)
│   ├── storage.py          DuckDB schema, upserts, ingest run log
│   └── ingest/
│       ├── github.py       GraphQL client, pagination, rate limits, row mapping
│       └── runner.py       incremental ingest orchestration
├── scripts/
│   └── record_fixtures.py  re-record test fixtures from a public repo
└── tests/
    ├── fixtures/           recorded GraphQL responses (pallets/markupsafe)
    ├── fakes.py            offline fake of the GitHub GraphQL API
    ├── test_cli.py
    ├── test_github.py
    ├── test_runner.py
    └── test_storage.py
```

Tests never touch the network; `tests/conftest.py` blocks sockets to make sure of it.

## Development

```bash
uv run pytest
uv run ruff check .
```

## License

MIT
