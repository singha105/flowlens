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

## Day 3 — 2026-10-29
- Done: flow metrics, `flowlens flow`, [METRICS.md](METRICS.md), and a synthetic ground-truth generator
  - `metrics/flow.py`, pure functions over DataFrames: lead and cycle time (p50/p85/p95 and a histogram, per type and per bug/incident/hotfix/other group), weekly throughput with zero weeks kept, daily WIP, aging WIP against the type's cycle-time p50/p85, cumulative flow (to do, in progress, done), and PR flow in four stages with the bottleneck and a monthly breakdown
  - `flowlens flow --repo owner/name --weeks 26` rebuilds the model, prints a compact table, and writes every series to `data/out/flow_<owner>__<name>.json` for the dashboard
  - `tests/synthetic.py`: a seeded team simulation (Poisson arrivals, exponential wait to start, lognormal cycle times and PR stages, never-started and not-planned issues, bots, abandoned and unreviewed PRs) with brute-force oracles written as plain loops
  - 254 offline tests (208 on Day 2). Every metric matches a brute-force count exactly, day by day and week by week; on a large history, cycle-time percentiles land within 10% of the true lognormal quantiles, throughput within 5% of the arrival rates, mean WIP within 5% of Little's law, and PR stage medians within 10%. Edge cases: no items, all in progress, same-day finishes, zero weeks, window boundaries, exclusions, abandoned PRs, never-started issues, starts dated after finishes, reviews after the merge. Ruff clean
  - Demo run, streamlit/streamlit, 26 weeks, 4.5 s:
    ```
    FlowLens flow · streamlit/streamlit · 26 weeks, 2026-04-27 → 2026-10-25 (as of 2026-10-29 05:55 UTC)

                                   n     p50     p85     p95
    Cycle time  issues           266    4.0d  150.0d  471.6d
                  bug            148    2.0d  161.0d  709.2d
                  incident        35    1.5d   10.0d  126.6d
                  other          118    6.0d  123.0d  284.1d
                PRs              884    1.1d    8.1d   38.1d
    Lead time   issues           370  264.3d 1059.2d 1616.9d
                  bug            189    7.0d  795.9d 1296.8d
                  incident        36    2.2d   35.1d  167.7d
                  other          181  473.8d 1304.2d 1845.1d
                PRs              884    1.1d    7.7d   38.0d

    Throughput  issues 14.2/week (min 1, max 55) | PRs 34.0/week (min 8, max 99) | weeks with none: 0
    WIP today   341 (issues 142, PRs 199) | aging > p85: 241 items (oldest issue #637, 2341 days)
    PR flow     medians: first commit→open 1m | open→first review 28h | review→approve 0m | approve→merge 4h
    Bottleneck  waiting for first review (89% of the summed stage medians)
    ```
- PM observations (streamlit, 2026-04-27 to 2026-10-25):
  1. **A delivery SLE works for PRs and regressions, not for issues as a whole.** 85% of merged PRs finished within 8.1 days of their first commit (86% within 9), so "85% of PRs merged within 9 days" is supportable. Regressions (`type:regression`, the incident stand-in) are tight too: 85% fixed within 10 days. All issues together aren't: p50 is 4.0 days but p85 is 150, because the window includes backlog clean-up. 161 of the 370 issues closed were over a year old, and 55 closed in the week of 2026-07-20 alone.
  2. **Waiting for first review is the PR bottleneck.** A median of 28 hours, 89% of the summed stage medians. Once a human reviews, the first review is usually the approval (504 of 884 PRs), and the merge follows about 4 hours later. A review rotation or a first-response target would shorten PR cycle time more than faster coding would. The AI reviewer is already changing this: merged PRs with a human review fell from about 100% through July to 27% in October, while merges rose to 222 that month (99 in the week of 2026-10-05). Day 5's change failure rate should check whether quality moved with it.
  3. **WIP is mostly stale, so clean it up before using it as a signal.** 341 items are in progress against 48 delivered a week. By Little's law that's about 7 weeks in progress on average, while the median PR finishes in 1.1 days. 143 of the 199 open PRs are drafts and 167 are older than the PR p85 of 8.1 days; 74 in-progress issues are older than the issue p85 of 150 days, and the oldest was assigned 2,341 days ago (#637). Closing stale drafts and unassigning abandoned issues would make the aging-WIP list a useful daily alert; a limit on open PRs would keep it that way.
- Decisions: the window is whole ISO weeks ending at the last Monday on or before the last successful ingest. The partial week is left out of throughput and durations, so Day 4 samples complete weeks only, and re-running on the same data gives the same numbers
  - Percentiles are nearest rank (`numpy.percentile`, `inverted_cdf`), never interpolated, so "85% within p85" holds exactly
  - Delivered means completed, closed with no reason, or merged. An abandoned PR leaves WIP when it closes but is never throughput
  - A day's WIP is the state at midnight UTC at its end, so an item started and finished the same day is never WIP. Cumulative flow's "done" starts at 0 at the window start
  - Durations are calendar days in UTC (PRD open question 4)
  - PR stages: a review or approval after the merge counts as missing, each median is over the PRs that have both ends, and the bottleneck's share is of the summed stage medians, since medians don't add
  - pandas 3.1 and numpy 2.5 added as dependencies
- Known gaps: issue cycle time covers 72% of the issues delivered (the rest have no start signal), and PR coding time is invisible on streamlit, where the first commit is a median of 35 seconds before the PR opens
  - Draft time counts as waiting for review (PR timelines aren't ingested), and changes-requested rounds aren't split out of "in review"
  - The throughput total counts an issue and the PR that closed it separately; Day 4 should forecast one type at a time
  - No `--as-of` yet: the window always ends at the last ingest. Day 4's backtest needs past cut-offs, and the pure functions already take any window
  - PR size, in the PRD's original PR-flow definition, isn't reported yet (additions and deletions are already ingested)
  - The quality report still counts the whole ingested history, not the flow window
- Manual steps for Arnab: none
- Next: Day 4, Monte Carlo "when" and "how many" forecasts sampled from the weekly throughput series, with a backtest

## Day 4 — 2026-10-29
- Done: Monte Carlo forecasts, the backtest, `flowlens forecast` and `flowlens backtest`, and the Forecasting section of [METRICS.md](METRICS.md#forecasting)
  - `metrics/forecast.py`: `when` (weeks until N items are done, as dates) and `how_many` (items done by a date), both resampling the last `history_weeks` complete weeks with replacement, zero weeks included, vectorised with numpy (10,000 trials in about 1 ms for 40 items, 27 ms for 2,000). 50/85/95% answers, the full distribution, and a scaled run for a 20% throughput drop
  - `backtest`: from each of the latest 52 weeks with 26 weeks of history before and a whole horizon after, forecast 4 and 8 weeks ahead with 6, 12, and 26 weeks of history, then score the 50/85/95% forecasts against what was delivered (held rate, p50 MAE, p50 bias). 312 forecasts in 0.2 s
  - CLI: `flowlens forecast --repo owner/name --items 40` or `--by 2026-12-31`, and `flowlens backtest --repo owner/name`, each with `--type issue|pr|total`. Both write JSON to `data/out/` for the dashboard
  - Streamlit re-ingested back to 2024-10-28 so that the backtest has 104 measured weeks: 2,811 issues, 6,846 PRs, 47 releases, 694 deployments, 36 release-build runs; 349 API calls (GraphQL cost 482), 19 min
  - 292 offline tests (254 on Day 3): exact results under constant throughput, distributions matched against full enumeration for tiny histories, percentile definitions, ordering, fixed-seed repeatability, speed, a look-ahead test (changing every week after an origin leaves that origin's forecast unchanged), and calibration on stationary data. Ruff clean
  - Demo run, streamlit/streamlit, as of 2026-10-29 18:34 UTC:
    ```
    Forecast: 40 issues from 2026-11-02 (12 weeks of history, 10,000 trials)
      history: 2026-08-03 → 2026-10-25, 12.8 issues/week (min 6, max 21, 0 empty weeks)
      50%: by 2026-11-30 | 85%: by 2026-11-30 | 95%: by 2026-12-07
      If throughput drops 20%: 85% by 2026-12-07 (1 week later)

    Forecast: issues done from 2026-11-02 through 2026-12-27 (8 whole weeks; 12 weeks of history, 10,000 trials)
      50%: at least 102 | 85%: at least 89 | 95%: at least 81
      If throughput drops 20%: 85%: at least 71 (18 fewer)

    Forecast: 200 PRs from 2026-11-02 (12 weeks of history, 10,000 trials)
      50%: by 2026-12-07 | 85%: by 2026-12-14 | 95%: by 2026-12-21
      If throughput drops 20%: 85% by 2026-12-21 (1 week later)
    ```
- Backtest calibration (streamlit, 52 weekly origins per horizon, origins 2025-09-08 to 2026-09-28):

  | Type | Horizon | History | 50% held | 85% held | 95% held | p50 MAE | p50 bias | Mean actual |
  |---|---|---|---:|---:|---:|---:|---:|---:|
  | issues | 4 weeks | 6 weeks | 52% | 69% | 83% | 23.5 | +2.0 | 52.5 |
  | issues | 4 weeks | 12 weeks | 56% | 71% | 85% | 19.9 | +3.8 | 52.5 |
  | issues | 4 weeks | 26 weeks | 60% | 79% | 90% | 17.2 | +5.0 | 52.5 |
  | issues | 8 weeks | 6 weeks | 46% | 65% | 73% | 48.0 | +1.9 | 103.8 |
  | issues | 8 weeks | 12 weeks | 48% | 77% | 87% | 33.7 | +6.2 | 103.8 |
  | issues | 8 weeks | 26 weeks | 50% | 83% | 87% | 29.8 | +7.1 | 103.8 |
  | PRs | 4 weeks | 6 weeks | 71% | 79% | 85% | 32.5 | +9.9 | 123.6 |
  | PRs | 4 weeks | 12 weeks | 69% | 79% | 92% | 34.5 | +11.8 | 123.6 |
  | PRs | 4 weeks | 26 weeks | 65% | 83% | 88% | 33.8 | +13.0 | 123.6 |
  | PRs | 8 weeks | 6 weeks | 63% | 77% | 83% | 61.8 | +17.5 | 237.9 |
  | PRs | 8 weeks | 12 weeks | 60% | 79% | 87% | 59.7 | +20.2 | 237.9 |
  | PRs | 8 weeks | 26 weeks | 58% | 77% | 85% | 52.1 | +17.8 | 237.9 |

  - The 85% forecast is overconfident on streamlit: 65–83%, never 85%. With the default 12 weeks of history, the 95% forecast is the one that holds about 85% of the time (85–92%)
  - Misses come in runs after the pace changes: the December holidays, the spring 2026 PR slowdown after a strong February, and September 2026, when issue histories still held the July backlog clean-up
  - Bias is positive throughout because throughput rose over the period (PRs 27 a week in late 2025, 66.7 in October 2026), so the forecasts ran low on average
  - 26 weeks of history is the best setting for issues (79% and 83% held); for PRs no history length fixes a trend
  - Synthetic control: on stationary Poisson data, the same method holds 86% with 26 weeks of history and 76% with 6 (8 weeks out), so part of the gap is the method (resampling ignores uncertainty in the average) and part is streamlit changing pace
- Decisions: confidence means the same in both directions. The 85% date is the earliest date by which 85% of trials finished; the 85% count is the largest count 85% of trials reached. Dates take the high percentile, counts the low one
  - Forecasts start at the next Monday after the last ingest, and the history is the complete weeks before the start; the week in progress is in neither. *How many* counts whole weeks only, and dates are the Monday after the finishing week
  - The throughput series covers only weeks the ingest fully covers: from the first Monday on or after `--since` (anything finished then was updated then) to the last complete week
  - Unit per run (`--type`, default `issue`), never a mixed count; `total` is there but double-counts an issue and its PR
  - The 20% drop scales every historical week by 0.8: a systematic slowdown, not random lost items
  - The default seed is 0, so the same data gives the same forecast; the backtest seeds each forecast from (seed, horizon, history, origin)
  - No look-ahead by construction: `how_many` and `when` filter the history to weeks that ended by the start, and the backtest calls them unchanged
  - The *when* direction has no separate backtest: "N items within H weeks" fails exactly when fewer than N were delivered, so both directions share one calibration
- Known gaps: past weeks are seen as they look today. 37 of 1,306 delivered issues (2.8%) were reopened and closed again in a later week, and a label added later can exclude an item from an earlier week. The forecasts themselves never read a later week
  - No trend or regime awareness: a step change hurts until it ages out of the history window
  - Items are assumed similar in size; size isn't used
  - The default history stays at 12 weeks as the brief set it, though streamlit's own backtest favours 26 for issues
- Manual steps for Arnab: none
- Next: Day 5, DORA metrics (deployment frequency, lead time for changes, change failure rate, recovery time)

## Day 5 — 2026-10-29
- Done: DORA metrics, traceable to the deploys, PRs, and issues behind them; `flowlens dora`; the DORA section of [METRICS.md](METRICS.md#dora)
  - Change mapping: ingest now compares each successful deploy from `deployment_source` with the one before it (`GET /repos/{o}/{r}/compare/{base}...{head}`, every page) and keeps the commits in new `compares` and `compare_commits` tables, fetched once since commits never change. `model/changes.py` builds the `deployment_changes` model table: each merged PR and the first deploy whose range holds its merge commit or a commit naming it (`Title (#123)`, so cherry-picks into patch releases count)
  - `metrics/dora.py`: deployment frequency (per week and day, median gap, empty weeks), lead time for changes (first commit → the deploy that shipped it, p50/p85, coding and waiting-to-deploy split, waiting share), change failure rate (reverts, hotfix/incident labels on PRs and issues, failed deployment statuses, within `dora.failure_window_hours`, default 72), and failed deployment recovery time (to the deploy that ships the fix, or the incident closing)
  - Bands live in `flowlens.yaml` under `dora.bands`, each with its source; nothing is built in. Defaults: deploy frequency and lead time from the 2024 Accelerate State of DevOps Report clusters, change failure rate and recovery from the 2023 clusters, all labeled approximate (sources and reasons in METRICS.md)
  - `flowlens dora --repo owner/name --weeks 26` prints the four keys with bands, the failed deploys with each signal and how they recovered, and the signals that didn't count with the reason; it writes `data/out/dora_<owner>__<name>.json` (every deploy, shipped PR, and signal) and `data/out/dora_failures_<owner>__<name>.csv`
  - 361 offline tests (292 on Day 4). Synthetic histories with known answers: a revert counted against the deploy that shipped what it reverts, a hotfix chain, a week with no deploys, a PR that shipped in no deploy, failed statuses, incidents fixed by a deploy or by closing, window edges, revert title formats, every per-deploy range status, band edges, and config validation. Ruff clean
  - Streamlit: incremental ingest with the compare ranges, 82 API calls in 74 s: 46 compares, 4,380 commits, all complete. 4,208 PRs matched to the release that shipped them (4,091 by merge commit, 117 through cherry-picks), every PR merged to `develop` since the first release except the 175 merged after 1.66 was cut. Lead time p50/p85 and the deploy count were checked against independent SQL over the same tables
  - Demo run, streamlit/streamlit, as of 2026-10-29 18:53 UTC (the last ingest):
    ```
    DORA (26 weeks, 2026-04-27 → 2026-10-25, deployment source: releases)
    Deployment frequency: 0.54/week (median gap 12.9d)                band: medium
      14 deploys, 0.08/day; 14 of 26 weeks had none
    Lead time for changes: p50 12.5d, p85 27.7d                       band: medium
      864 PRs (plus 679 excluded, such as bots): coding p50 1.3d, waiting to deploy p50 8.4d; waiting is 60% of all lead time
      not deployed: 65 PRs merged to develop in the window, 12 into other branches
    Change failure rate: 50% (7 of 14 deploys; see dora_failures_streamlit__streamlit.csv)  band: low
    Failed deployment recovery time: median 14.9d (6 recovered, 1 not yet)  band: low
    ```
    The 7 failed releases (1.57.0, 1.58.0, 1.59.0, 1.59.2, 1.61.0, 1.64.0, 1.66.0) and their 15 regression issues are listed in the output and the CSV, each with how long after the release it was opened and what fixed it
- Observations:
  1. 60% of lead time is waiting for a release, not coding: a p50 of 1.3 days from first commit to merge, then 8.4 days merged and waiting. Releasing more often would cut lead time more than any coding change; the patch releases show it, with 1.59.0's regressions fixed in 45 hours by 1.59.1 against 30 and 39 days for 1.57.0 and 1.58.0, which waited for the next minor release
  2. The change failure rate measures the failure window as much as the releases. Streamlit has no incident or hotfix label, so `type:regression` issues stand in, and users report regressions days after a release: 21% of releases fail with a 24-hour window, 50% with 72 hours, 79% with 7 days. Attribution by time also blames the latest release: 2 of the 7 counted regressions that name a version count against 1.59.2 but name 1.59.0. The band says "low", but quote the rate with its window
- Decisions: a PR shipped in the first deploy whose compare range carries it, by merge commit or by a commit naming it, so cherry-picks ship with the patch release. The first deploy has no base, so what it shipped is unknown and says so
  - Reverts are attributed by cause (the deploy that shipped the reverted PR), labels by time (the last successful deploy before the PR or issue was opened). A revert opened before its PR shipped isn't a production failure
  - Recovery is the first fix for any of a deploy's signals: the deploy that ships the revert, hotfix, or an incident's linked PR; the issue closing only when there's no linked PR to wait for. A fix that ships in a deploy that fails too ends the first failure; the second is its own. Chaining them made streamlit's recoveries span unrelated regressions across three releases (70 days for 1.57.0), and the signals can't tell whether a later failure is the same one
  - Deployment frequency counts successful deploys only; change failure rate's denominator is every deploy, failed ones included
  - Lead time's split is reported as p50s plus a waiting share (sum of waiting over sum of lead time), because the p50s of the parts don't add up to the p50 of the whole
  - Bands have no built-in default: they come from a report, so they sit in `flowlens.yaml` beside their source, and without them `flowlens dora` prints none. The latest DORA report (2025) dropped the four levels for seven archetypes, so the most recent levels are 2024's, and 2023's where 2024's overlap (medium's 10% failure rate below high's 20%; high and medium both under a day to recover). Values checked against Octopus Deploy's cluster tables; the report PDFs weren't read
  - Recorded test fixtures are trimmed to commit subject lines: streamlit's commit messages carry AI co-author trailers, which don't belong in this repo. The two Day 2 workflow-run fixtures were trimmed the same way (4 messages)
- Known gaps: deployment rework rate, DORA's fifth metric since 2024, isn't computed (patch releases would be a natural signal)
  - Releases stand in for production deploys; for a library like streamlit that means "available on PyPI", not "running in production"
  - Labels drive the failure rate: no labels means 0%, liberal labeling means more. Incident issues are attributed by time, not by the version they name
  - Revert detection needs the reverted PR in the title or the squash commit; 3 of streamlit's 8 revert PRs name nothing FlowLens can match. PR bodies ("Reverts owner/repo#123") aren't ingested
  - Recovery ends when the fix ships, not when users are restored, which no repository records
  - Compares are fetched for the configured source only; switching `deployment_source` needs an `ingest`
- Manual steps for Arnab: none
- Next: Day 6, the Streamlit dashboard, the HTML report, GitHub Pages, and CI
