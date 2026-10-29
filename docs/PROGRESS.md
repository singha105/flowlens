# FlowLens progress log

## Day 1 — 2026-10-29
- Done: PRD, scaffold (uv, src layout, ruff, pytest), issue/PR ingestion into DuckDB, CLI `ingest`
  - GraphQL client with cursor pagination, rate-limit pacing from `rateLimit`, and exponential backoff on 403/429/5xx and network errors
  - Incremental re-runs, `--since` history floor, `--full` override, every run logged in `ingest_runs`
  - 59 offline tests on recorded fixtures from pallets/markupsafe; ruff clean
  - Smoke test, pallets/markupsafe full history: 148 issues, 384 PRs, 10 API calls (rate-limit cost 20), 29.0 s. Re-run: incremental, 1 issue + 1 PR, 2 calls, 5.3 s
- Decisions: GraphQL over REST for issues/PRs (fewer calls); DuckDB file per repo
  - Items are read newest-updated first. Issues filter `since` server-side; PRs stop paging at the first older item. An item updated mid-crawl can repeat but nothing unchanged gets skipped.
  - Re-runs resume from the last successful run's newest `updatedAt` minus 5 minutes. Asking for older history than the database holds triggers a full fetch automatically.
  - Upserts by number never let an older snapshot overwrite a newer one; failed runs keep what they fetched but don't move the watermark.
  - The raw GraphQL node is kept in a JSON column so Day 2's canonical model can derive new fields without refetching.
  - First review = earliest submitted review by someone other than the PR author (replying on your own PR creates a review). First commit uses the author date, which survives rebases.
  - Timestamps stored as naive UTC. PR page size 50: GitHub takes about 2 s per page, upserts take 0.03 s.
- Known gaps: no timeline events yet (Day 2), and no releases, deployments, or workflow runs (Day 2)
  - First review is taken from a PR's first 20 reviews; labels are capped at 50 and assignees at 20 per item
  - A mistyped repo leaves an empty database behind, with the failed run logged
  - markupsafe is a poor demo repo for PR flow: 197 of 384 PRs are from bots and only 13 have a review from someone else. Worth weighing in Day 2's demo repo choice.
- Manual steps for Arnab: none
- Next: Day 2
