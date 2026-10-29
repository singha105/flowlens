# 4. Local-first and $0: a DuckDB file, the user's own token, and a static report

- Status: accepted
- Date: 2026-10-29 (decided on Days 1 and 6, written down on Day 7)

## Context

The PRD's engineering manager can't get a new vendor approved in under a quarter, and the product manager needs a link, not an install. Commercial delivery analytics products need org-wide access and a security review. The project's own constraint is $0 to build and to run.

## Decision

- FlowLens runs on the user's machine with the user's GitHub CLI login: the token is read from `gh auth token` at run time and never written to disk.
- Each repo is one DuckDB file. Ingest is incremental, so a re-run fetches only what changed.
- Sharing is a static page: `flowlens report` writes one self-contained HTML file with plotly inlined. A GitHub Actions workflow rebuilds it weekly with the built-in `GITHUB_TOKEN` and publishes it to GitHub Pages.
- The Streamlit dashboard only reads the databases. Hosted on Streamlit Community Cloud, it downloads the public database the workflow publishes, so it needs no token and no secrets.

## Consequences

- Nothing to host or pay for. On streamlit, a one-year first ingest took 7.3 minutes and 318 GraphQL points (6% of the hourly budget), and a re-run takes seconds. A two-year ingest took 19 minutes and 482 points, which fits the 1,000 points an hour a workflow's `GITHUB_TOKEN` gets.
- The report is about 5 MB because plotly.js is inlined; in exchange it opens offline, from an attachment, or on a phone.
- The hosted dashboard shows the snapshot it downloaded at start-up until it's rebooted; the weekly page is the source of truth.
- No multi-user service: anyone who wants their own repo runs the CLI, which a fresh clone does in about 20 seconds for a small repo.
