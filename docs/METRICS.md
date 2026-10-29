# Flow metrics

What `flowlens flow` measures, how, from which GitHub signals, and where each number is weaker than it looks. The code is [src/flowlens/metrics/flow.py](../src/flowlens/metrics/flow.py); every metric has a ground-truth test in [tests/test_flow.py](../tests/test_flow.py).

Coverage figures below are from the demo repo, streamlit/streamlit, over the 26 weeks from 2026-04-27 to 2026-10-25 (run on 2026-10-29). Run `flowlens quality` and `flowlens flow` to get them for another repo.

## Shared conventions

**Work items.** Issues and PRs from the canonical model (`work_items`, built on Day 2). Each has `created_at`, an inferred `started_at` (or none), a `done_at` (or none), and an outcome.

**Counted.** Everything not excluded. Excluded items are bot-authored items, issues closed as not planned or duplicate, and items with an excluded label (`flowlens.yaml`). They stay in the model but no metric counts them. On streamlit that removes 2,045 of 5,716 items, 1,954 of them from bots.

**Delivered.** Counted, done, and the outcome is `completed`, `closed_no_reason` or `merged`. An abandoned PR (closed without merging) is finished but not delivered.

**Window.** `--weeks N` whole ISO weeks, Monday 00:00 UTC to Monday 00:00 UTC, ending at the last Monday on or before *as of*. *As of* is when the last successful ingest finished (or now, if there wasn't one), so a re-run on the same data gives the same answer. The current, unfinished week is left out of everything selected by finish date (lead and cycle time, throughput, PR flow). Daily series (WIP, cumulative flow) run up to *as of*, partial day included.

**Days and times.** Everything is UTC. Durations are fractional days (or hours for PR stages), not working days: a weekend counts.

**Percentiles.** Nearest rank: p85 is the smallest value with at least 85% of the values at or below it. It is always a real observation, never interpolated, so "85% of items finished within p85" is literally true. This is `numpy.percentile(..., method="inverted_cdf")`.

**Histograms.** Days in bins `[0, 1)`, `[1, 2)`, `[2, 4)`, `[4, 7)`, `[7, 14)`, `[14, 30)`, `[30, 60)`, `[60, 90)`, `[90, 180)` and 180+.

**Label groups.** Lead and cycle time are also given per label kind: `bug`, `incident`, `hotfix` (which labels map to which kind is in `flowlens.yaml`), plus `other` for items with none of them. An item labelled both bug and incident is in both groups. Streamlit has no incident label; `type:regression` stands in for it.

**History.** Ingest keeps items *updated* since `--since`. An item that was opened earlier and not touched since isn't in the data at all, and the report says so.

## Lead time

**Definition.** How long a delivered item took from being opened to being done.

**Formula.** `lead = done_at − created_at`, in days, for every item delivered in the window, then p50/p85/p95 and a histogram per type and label group.

**GitHub signals.** Issue `createdAt` and the time of its last close; PR `createdAt` and `mergedAt`.

**Limitations.**
- For issues, lead time includes time in the backlog, and on an old project that can be years. On streamlit 161 of the 370 issues delivered in the window (44%) were opened more than a year before they closed, and 198 (54%) had a lead time over 180 days, so the issue p50 is 264 days. Treat it as the age of the backlog being cleared, not as delivery speed.
- A reopened item counts once, finishing at its last close; the time it spent closed in between still counts.
- For PRs, lead time starts when the PR was opened, not when work started; see cycle time.

## Cycle time

**Definition.** How long a delivered item took from work starting to being done.

**Formula.** `cycle = done_at − started_at`, in days, for every item delivered in the window that has a start. Same percentiles, histograms and groups as lead time. A negative cycle time (a PR whose first commit is dated after its merge) counts as 0.

**GitHub signals.** `started_at` is inferred, never reported by GitHub:
- an issue starts at the earliest of its first assignment, the opening of its first linked PR (closing keyword, manual link, or the PR that closed it), or an `in progress` label; never before it was opened, and signals after it closed are ignored;
- a PR starts at the earliest of its first commit's author date and its opening.

**Limitations.**
- **Issue `started_at` is inferred, and coverage is 72%.** 266 of the 370 issues delivered on streamlit had a start signal (186 from a linked PR, 80 from an assignment); the other 104 closed with no sign anyone worked on them, so they have a lead time but no cycle time. Across all counted issues, including open ones, only 51% have a start. Issue cycle time describes the work that was tracked, not all the work.
- Assignment can happen long before work begins, and a linked PR can be opened late in the work, so the start is a proxy either way. Old issues assigned years ago and closed in the window give issue cycle time a long tail (streamlit: p50 4.0 days, p85 150 days).
- A PR's first commit is its author date, which survives rebases but not squashes or `commit --amend --reset-author`. On streamlit the first commit is a median of 35 seconds before the PR opens (719 of 884 merged PRs within 5 minutes): contributors squash and push at once, so PR cycle time is close to PR lead time and coding time before the PR is invisible.
- Time is calendar time, including nights, weekends and time a PR sat as a draft.

## Throughput

**Definition.** Items delivered per ISO week.

**Formula.** For each week in the window, the number of delivered items whose `done_at` falls in `[Monday, next Monday)`, per type and in total. Weeks with nothing delivered are kept as 0, because Day 4's Monte Carlo forecast samples from this series and dropping zero weeks would make the forecast optimistic. The summary gives the mean, min, max and the number of zero weeks.

**GitHub signals.** Issue close time with a completed (or no) reason; PR `mergedAt`.

**Limitations.**
- The total counts an issue and the PR that closed it as two items. Forecast issues and PRs separately, or pick one unit.
- One item is one unit, whatever its size.
- Bulk closes show up as spikes: streamlit closed 55 issues in the week of 2026-07-20, most of them old backlog, against a mean of 14.2.

## WIP per day

**Definition.** Items in progress at the end of each day.

**Formula.** At each day's cut (midnight UTC at its end, or *as of* for today), the counted items with `started_at < cut` and no `done_at` before the cut, per type and in total. Leaving WIP needs any finish, so an abandoned PR leaves when it's closed. An item that starts and finishes on the same day is never in WIP. "WIP today" is the last day's count.

**GitHub signals.** The same start signals as cycle time; close and merge times.

**Limitations.**
- Items with no start signal are never in WIP, so issue WIP undercounts work that isn't assigned or linked.
- A PR is in progress from its first commit, which is only known once the PR is opened: work on branches without a PR is invisible until then.
- Draft PRs count as in progress. On streamlit 143 of the 199 PRs in progress are drafts.
- Long-idle items not updated since `--since` are missing (see History).
- GitHub has no "stopped working" signal, so an item someone abandoned without closing or unassigning stays in WIP forever. That's what aging WIP is for.

## Aging WIP

**Definition.** How long each item in progress has been in progress, against how long finished items of its type took.

**Formula.** For every counted item in progress at *as of*: `age = as_of − started_at`, compared with that type's cycle-time p50 and p85 over the window. `over_p85` flags items already older than 85% of finished items took; they're the most likely to be stuck. Listed oldest first.

**GitHub signals.** As for WIP.

**Limitations.**
- The comparison is only as good as the cycle-time distribution: with a long-tailed p85 (streamlit issues: 150 days), "over p85" flags only the truly ancient. Compare per type; PRs and issues have very different distributions.
- If nothing finished in the window, there is no p85 and nothing is flagged.
- Age keeps growing over weekends and holidays.

## Cumulative flow

**Definition.** Daily counts of items to do, in progress and done.

**Formula.** At each day's cut, per type and in total:
- to do: opened before the cut, not started and not finished before it;
- in progress: exactly WIP;
- done: delivered since the window started (so the done line starts at 0 instead of at the whole history).

An item that finishes without being delivered (an abandoned PR) leaves the chart. A PR whose first commit predates its opening goes straight to in progress.

**GitHub signals.** Open, start, close and merge times.

**Limitations.**
- "To do" is every open issue without a start signal, which on most repos is a backlog of feature requests rather than committed work. On streamlit it's 380 issues.
- Excluded items (not planned, duplicates, bots) never appear, including while they were open.
- Long-idle open items are missing (see History), so "to do" is a lower bound.

## PR flow breakdown

**Definition.** Where a merged PR's time goes, in four stages, and which stage is the bottleneck.

**Formula.** For every counted PR merged in the window:

| Stage | From | To |
|---|---|---|
| coding | first commit | PR opened |
| waiting for first review | PR opened | first review |
| in review | first review | first approval |
| waiting to merge | first approval | merge |

Durations are in hours. Each stage's median is over the PRs that have both ends; per-stage counts are reported. A review or approval after the merge didn't hold the PR up, so it counts as missing. A negative stage (a first commit dated after the PR opened) counts as 0. The **bottleneck** is the stage with the longest median, and its share is that median over the sum of the four stage medians. The JSON also has a monthly breakdown with the share of merged PRs that had a human review.

**GitHub signals.** The first commit's author date; PR `createdAt`; the earliest submitted review and the earliest approval by someone other than the author and not a bot; `mergedAt`.

**Limitations.**
- **Unreviewed PRs drop out of the review stages.** On streamlit 272 of 884 merged PRs (31%) had no human review before merging. The monthly breakdown shows why: human-reviewed PRs were 98-100% through July 2026, then 85% in August, 46% in September and 27% in October, after a bot's AI review took over. Bot reviews never count, so the review stages describe a shrinking minority of PRs from August on.
- **The first review is usually the approval.** In 504 of 884 streamlit PRs they're the same review, so "in review" has a median of 0. Changes requested and comment-only rounds aren't split out.
- **Coding time is mostly invisible** where contributors squash before pushing (see cycle time).
- Draft time counts as waiting for review; PR timelines (ready-for-review events) aren't ingested.
- Stage medians don't add up to the median PR's total time, which is why the share is "of the summed stage medians".

## What the report adds

`flowlens flow` prints notes under the table when the data is thin: issue cycle-time coverage, merged PRs with no human review, a window that starts before the ingested history, and the history caveat. The JSON in `data/out/flow_<owner>__<name>.json` holds every series: lead and cycle summaries per type and group, weekly throughput, daily WIP, cumulative flow per type, the aging list, PR flow with its monthly breakdown, coverage counts, and the notes.

## How the metrics are tested

`tests/synthetic.py` simulates a team: Poisson arrivals, an exponential wait before work starts, lognormal cycle times, never-started and not-planned issues, bots, abandoned and unreviewed PRs, lognormal PR stages, all cut off at *as of* the way an ingest sees them. The tests check:

- **exactly**, against brute-force loops over the same items: percentiles per type and group, histogram counts, every week's throughput, every day's WIP and cumulative flow, the aging list, and PR stage medians and bottleneck;
- **statistically**, on a year at high volume: cycle-time p50/p85/p95 within 10% of the lognormal's true quantiles, throughput within 5% of the arrival rates, mean WIP within 5% of Little's law (L = λW), PR stage medians within 10%, and the right bottleneck;
- **edge cases** by hand: no items, all items in progress, items finished the day they started, weeks with zero throughput, window boundaries, excluded items, abandoned PRs, never-started issues, starts dated after finishes, reviews after the merge.
