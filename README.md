# FlowLens

FlowLens turns a GitHub repository's history into the numbers delivery conversations actually need: cycle time, throughput, and aging work in progress; Monte Carlo forecasts that answer "when will these 30 items be done?" with a date range and a confidence level; and DORA metrics built from real merges, releases, and deployments. It runs locally on your own `gh` credentials, stores everything in a DuckDB file, writes every metric definition down, and costs nothing to run.

> Status: early development (Day 3 of 7). Ingestion, the canonical work-item model, a data-quality report, and flow metrics work; forecasts, DORA metrics, and the dashboard are on the way. See [docs/PRD.md](docs/PRD.md) and [docs/PROGRESS.md](docs/PROGRESS.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and the [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`.

```bash
git clone https://github.com/singha105/flowlens && cd flowlens
uv run flowlens ingest --repo pallets/markupsafe --since 2025-10-01
uv run flowlens quality --repo pallets/markupsafe
uv run flowlens flow --repo pallets/markupsafe --weeks 26
```

`ingest` writes `data/pallets__markupsafe.duckdb`: issues with their timelines, PRs with their reviews, releases, GitHub Deployments, and the runs of any Actions workflow you name with `--workflow`. Run it again and only what changed since the last run is fetched; `--full` refetches everything from `--since`. It then rebuilds the model: one `work_items` row per issue or PR with when it started and finished, a `work_item_events` history, and a `deploys` stream.

`quality` reports how much of that history FlowLens can actually measure: how many items have a start signal, how many closed without a reason or without ever starting, how many merged PRs had a human review, and which deployment signals exist. Low coverage is a finding in its own right, so the report spells it out.

`flow` computes the flow metrics over the last `--weeks` whole ISO weeks: lead and cycle time percentiles (p50, p85, p95) per type and label group, weekly throughput, daily WIP, aging WIP, cumulative flow, and where merged PRs spend their time. It prints a summary and writes every series to `data/out/flow_<owner>__<name>.json`. Each metric's definition, formula, GitHub signals, and limitations are in [docs/METRICS.md](docs/METRICS.md).

The token is read from `gh auth token` each time FlowLens runs and is never written to disk.

## Configuration

`flowlens.yaml` in the working directory says how to read a repository; every key is optional and the shipped file lists the defaults:

- `started_signal`: what counts as work starting on an issue. Default: the earliest of first assignment, the first linked PR being opened, or an `in progress` label.
- `labels`: which labels mark a bug, an incident, or a hotfix.
- `deployment_source`: `releases`, `deployments` (to `deployment_environments`), or `workflow:<name>`.
- `exclude`: items kept but left out of metrics: closed as not planned or duplicate, duplicate/wontfix/invalid labels, and bot authors.

A `repos: owner/name:` section overrides any of these for one repository. Changing the file takes effect on the next `ingest`, `quality`, or `flow` run, without refetching.

## Layout

```
flowlens/
├── flowlens.yaml           settings: start signals, labels, deploy source, exclusions
├── docs/
│   ├── PRD.md              product requirements
│   ├── METRICS.md          metric definitions and their limitations
│   ├── PROGRESS.md         daily build log
│   └── decisions/          decision records (0001: the demo repo)
├── src/flowlens/
│   ├── cli.py              `flowlens ingest`, `quality`, and `flow` (typer)
│   ├── config.py           flowlens.yaml loading and validation
│   ├── storage.py          DuckDB schema, upserts, migrations, ingest run log
│   ├── quality.py          data-quality report
│   ├── ingest/
│   │   ├── client.py       GitHub client: gh auth, retries, rate-limit pacing
│   │   ├── github.py       issues, timelines, PRs, reviews (GraphQL)
│   │   ├── delivery.py     releases, deployments (GraphQL), workflow runs (REST)
│   │   └── runner.py       incremental ingest with per-stream watermarks
│   ├── model/
│   │   ├── work_items.py   canonical work items and the start/finish rules
│   │   ├── deploys.py      deploys from the configured source
│   │   └── build.py        rebuilds the model tables from the source tables
│   └── metrics/
│       ├── flow.py         flow metrics as pure functions over DataFrames
│       └── report.py       the `flow` report: JSON and the printed summary
├── scripts/
│   ├── record_fixtures.py  re-record test fixtures from public repos
│   └── compare_repos.py    the demo-repo comparison in docs/decisions/0001
└── tests/
    ├── fixtures/           recorded API responses (markupsafe, streamlit, cli)
    ├── fakes.py            offline fakes of the GraphQL and REST APIs
    ├── synthetic.py        seeded synthetic histories with brute-force ground truth
    └── test_*.py
```

Tests never touch the network; `tests/conftest.py` blocks sockets to make sure of it.

## Development

```bash
uv run pytest
uv run ruff check .
```

## License

MIT
