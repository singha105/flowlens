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

## Day 2 — 2026-10-29
- Done: richer ingestion, `flowlens.yaml`, the canonical work-item model, `flowlens quality`, the demo repo choice, and its 12-month ingest
  - Ingestion: issue timelines (assigned/unassigned, labeled/unlabeled, cross-referenced, connected, closed with its state reason, reopened); PR reviews with first review, first approval, review count, merge commit SHA, and closing issues; releases; GitHub Deployments with their status history; Actions workflow runs over REST for any workflow named in the config or with `--workflow`
  - One `GitHubClient` for GraphQL and REST, with the same retries and pacing; REST pages follow `Link` headers, and run searches over GitHub's 1,000-result cap are split in half until they fit
  - Model: `work_items` (issue or PR, created/started/done, outcome, labels, bug/incident/hotfix kinds, linked items, reopen count, exclusion), `work_item_events`, and `deploys`, rebuilt from the source tables on every `ingest` and `quality` run
  - `flowlens quality` prints coverage and plain-language findings; demo repo comparison in [decisions/0001-demo-repo.md](decisions/0001-demo-repo.md), reproducible with `scripts/compare_repos.py`
  - Demo ingest, streamlit/streamlit, 12 months: 1,340 issues, 4,376 PRs, 23 releases, 332 deployments, 35 release-build runs; 233 API calls (GraphQL cost 318), 7.3 min; re-run 6 calls, 8 s. 5,716 work items: 83% with a start, 2,045 excluded (1,954 from bots), 33% of finished issues closed without ever starting, 83% of merged human PRs reviewed, median 26 h to first review, 23 deploys. 20 random issues and 20 PRs matched fresh fetches exactly
  - The Day 1 markupsafe database migrated in place and refetched automatically (17 calls, 33 s)
  - 208 offline tests (59 on Day 1) on fixtures recorded from markupsafe, streamlit, and cli/cli, covering each event type and every start rule; ruff clean
- Decisions: an issue starts at the earliest of first assignment, the first linked PR being opened, or an `in progress` label; signals after it finished are ignored; it never starts before it was opened; with no signal it never started. Ties go to config order.
  - A linked PR follows GitHub's Development sidebar: a closing keyword, a manual connection, the PR that closed the issue, or the PR listing it in its closing references. Same repo only; plain mentions don't count. GitHub reports `willCloseTarget: false` once the PR merges, so the closer on the close event matters most.
  - A PR starts at its first commit's author date, else when it was opened, and isn't clamped: commits before the PR are part of the work. Reopened items are done at their last close.
  - Bots (GitHub's `Bot` type) are excluded by default and their reviews never count as a first review or approval; Copilot, AI reviewers, and github-actions all review within minutes and would otherwise make review times meaningless.
  - Deploys come from one configured source, releases by default: published, non-draft, non-prerelease releases at publish time. Deployments count at their first SUCCESS status; workflow runs at completion, success or failure.
  - Each stream keeps its own watermark. Releases, deployments, and workflow runs are read by creation time but change afterwards, so the last three days are re-read. A workflow added to the config later is fetched over all stored history.
  - `FETCH_VERSION` forces a full refetch when the queries change. Day 1 databases are migrated in place and refetched automatically.
  - Issue pages are 25, not 50: GitHub silently drops timeline events from bigger issue pages (52 of 100 streamlit issues were missing events at 50, with no error, `hasNextPage` false, and `totalCount` dropping too). Pages of 10 and 25 matched single-issue fetches on 300 issues, PR pages of 50 matched on 100 PRs, and a closed issue with no close event is re-read on its own as a backstop. The first demo-repo comparison was skewed by this and was redone.
- Known gaps: deployment statuses are read 20 per deployment (the latest state stands in beyond that); labels 50, assignees 20, and closing references 10 per item
  - PR timelines aren't fetched, so a PR's reopen count is always 0 and draft-to-ready time isn't known
  - A new cross-reference may not bump an issue's `updatedAt`, so an incremental run can miss a link until the issue next changes (closing it does)
  - Releases are read by creation time, so a release drafted more than three days before it's published is missed by incremental runs until a `--full` run
  - Bot accounts that are plain users (service accounts) count as people, and one person with two accounts can review their own PR
  - The page-size fix is measured, not guaranteed: GitHub documents no such limit, and only the closed-without-close-event case is caught automatically
  - Streamlit has no incident label; `type:regression` stands in until change failure rate is defined on Day 5
  - Streamlit replaced most human reviews with a bot's AI review from July 2026 (human-reviewed merged PRs: 100% to 30% by October; bot approvals 0% to 78%). Review metrics need a before/after split, not one annual number
  - The quality report counts every ingested item, including old items updated in the window; metrics on Day 3 should window by when items finished
- Manual steps for Arnab: none
- Next: Day 3, flow metrics (cycle time, throughput, WIP, aging) from `work_items`
