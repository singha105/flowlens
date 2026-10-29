# Flow metrics

What `flowlens flow` measures, how, from which GitHub signals, and where each number is weaker than it looks. The code is [src/flowlens/metrics/flow.py](../src/flowlens/metrics/flow.py); every metric has a ground-truth test in [tests/test_flow.py](../tests/test_flow.py).

Coverage figures below are from the demo repo, streamlit/streamlit, over the 26 weeks from 2026-04-27 to 2026-10-25 (run on 2026-10-29 with one year of history ingested; the forecasting figures use a later two-year ingest). Run `flowlens quality` and `flowlens flow` to get them for another repo.

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

## Forecasting

`flowlens forecast` answers two questions with a Monte Carlo simulation over weekly throughput: **when** will N more items be done (`--items`), and **how many** will be done by a date (`--by`). `flowlens backtest` replays the same forecast from past weeks and checks it against what actually happened. The code is [src/flowlens/metrics/forecast.py](../src/flowlens/metrics/forecast.py).

### How it works

1. **Throughput series.** Items delivered per ISO week for one type (`--type issue`, `pr` or `total`), zero weeks included, over every week the ingest fully covers: from the first Monday on or after `--since` (an item finished then was updated then, so it's in the data) to the last complete week before the last ingest.
2. **History.** The last `--history-weeks` complete weeks (12 by default) that ended by the forecast's start. The start defaults to the next Monday; the week in progress is neither history nor forecast.
3. **Trials.** 10,000 by default, all drawn at once with numpy (10,000 trials take a few milliseconds). Each trial draws weeks from the history with replacement:
   - *when*: weeks are drawn until the running total reaches N. A trial that needs k weeks is done before `start + k weeks`, the Monday after its finishing week.
   - *how many*: the whole weeks from the start through `--by` are drawn and summed. Days after the last whole week aren't counted, which errs on the cautious side.
4. **Confidence.** For *when*, the 85% date is the earliest date by which 85% of trials had finished. For *how many*, the 85% count is the largest count that 85% of trials reached or beat. So the cautious answer is the **high** percentile for a date and the **low** one for a count: "85% chance of at least 46 items".
5. **Sensitivity.** Every forecast is repeated with every historical week scaled by 0.8, which answers "what if throughput drops 20%?" (someone leaves, a release freeze) as a shift in the 85% date or count.
6. **Reproducible.** The random seed defaults to 0, so the same data gives the same forecast; `--seed` changes it.

### Why throughput, not story-point velocity

- **It needs no estimates.** Items finished per week is already in GitHub's history. Points have to be estimated by people, drift over time (point inflation), and mean different things on different teams.
- **It keeps the variation.** An average velocity turns a noisy history into one number and one date. Resampling real weeks, slow and empty ones included, gives a range with a confidence level, and the backtest can check that range against reality. An average can't be checked that way.
- **It counts in the backlog's unit.** "When will these 40 issues be done?" is answered in issues, with no conversion from points.
- Practitioners in flow-based delivery (Daniel Vacanti, Troy Magennis) argue that once items are split to roughly similar sizes, counting them forecasts about as well as summing estimates, without the estimation cost.

### Assumptions

- **The future resembles the recent past:** same team, same process, same mix of work. That's why the history is the *recent* weeks.
- **Items are roughly similar in size,** or at least the backlog has the same mix of sizes as the history. One huge item counts the same as one typo fix.
- **Weeks are independent:** a big week doesn't predict the next one.
- **Scope is fixed.** Work that gets split or added while you wait means more items than N.
- **The unit matches.** Forecast issues from issue throughput and PRs from PR throughput. `total` counts an issue and the PR that closed it as two items.

### When it breaks

- **Trends and step changes.** The history lags when a team grows, shrinks, or changes how it works: a new AI reviewer, a release freeze, a hiring wave. The backtest shows how much this costs on a given repo.
- **Bulk clean-ups.** Closing dozens of stale issues in one week inflates issue throughput without delivering anything. A history containing such a week forecasts too much.
- **Short histories are overconfident.** Resampling a few weeks treats them as the whole truth, so uncertainty about the average itself never enters the forecast. On stationary synthetic data (Poisson, 20 a week), the 85% forecast eight weeks out held 76% of the time with 6 weeks of history, 81% with 12, 86% with 26, and 87% with 200.
- **Long horizons** compound every problem above.
- **Very low throughput.** If most weeks deliver nothing, the 85% count is 0 and the 85% date is far away. That answer is honest, but it isn't useful.

### Backtesting

For each horizon (4 and 8 weeks), the backtest takes the latest 52 weeks that have 26 weeks of history before them and a whole horizon after. From each such week (the *origin*) it forecasts "how many in the next H weeks" with 6, 12, and 26 weeks of history. Every history length is scored on the same origins. Each forecast:

- **sees only weeks that ended by its origin.** The forecast function itself filters the history, and a test changes every week after an origin and checks that the forecast from that origin doesn't move;
- **is compared with the actual count** delivered in those H weeks. It *held* if at least its count was delivered.

Reported per horizon and history length: how often the 50%, 85%, and 95% forecasts held (well calibrated: about 50%, 85%, and 95%; higher means too cautious, lower means overconfident), the mean absolute error of the 50% forecast, and its bias (actual minus forecast; positive means the forecasts ran low).

The *when* forecast needs no separate backtest. "N items done within H weeks" fails exactly when fewer than N were delivered in H weeks, so the two directions share one calibration.

**Look-ahead.** No forecast reads a week after its origin. The one leak left is that past weeks are seen as they look *today*: an issue reopened after an origin counts at its final close, and a label added later (duplicate, wontfix) can remove an item from an earlier week. On streamlit, 37 of the 1,306 issues delivered over the 104 measured weeks (2.8%) were closed, reopened, and closed again in a later week, so at the time they counted in a different week. A merged PR can't be reopened.

### On the demo repo

streamlit/streamlit, 104 measured weeks (2024-10-28 to 2026-10-25), 52 origins per horizon, 10,000 trials per forecast. How often the 85% forecast held:

| History | Issues, 4 weeks | Issues, 8 weeks | PRs, 4 weeks | PRs, 8 weeks |
|---|---:|---:|---:|---:|
| 6 weeks | 69% | 65% | 79% | 77% |
| 12 weeks (default) | 71% | 77% | 79% | 79% |
| 26 weeks | 79% | 83% | 83% | 77% |

- **The forecast is overconfident here.** No configuration reaches 85%, and short histories are worst: issues, 4 weeks out, with 6 weeks of history held 69% of the time. On this repo, the 95% forecast (which held 85–92% with 12 weeks of history) is the one to quote as an "85% sure" commitment.
- **Misses come in runs after the pace changes:** the December holidays; March to May 2026, when PR throughput fell after a strong February (about 160 PRs per four weeks); and September 2026, when issue histories still held the July backlog clean-up (about 100 issues closed in four weeks) and forecast too many.
- **Forecasts ran low on average.** The p50 bias is positive everywhere (+2 to +7 issues, +10 to +20 PRs) because throughput rose over the period, with PRs at 66.7 a week in October 2026 against 27 a week in late 2025.
- **More history helps issues** (79% and 83% with 26 weeks), whose throughput is noisy week to week, but not PRs, whose pace has trended; a longer history only remembers an older pace. The p50's typical miss is a third to almost half of the actual count for issues, and about a quarter for PRs.

`flowlens backtest` reruns this for any repo, so the right history length and confidence level can be chosen from that repo's own record rather than from a default.

## What the reports add

`flowlens flow` prints notes under the table when the data is thin: issue cycle-time coverage, merged PRs with no human review, a window that starts before the ingested history, and the history caveat. The JSON in `data/out/flow_<owner>__<name>.json` holds every series: lead and cycle summaries per type and group, weekly throughput, daily WIP, cumulative flow per type, the aging list, PR flow with its monthly breakdown, coverage counts, and the notes.

## How the metrics are tested

`tests/synthetic.py` simulates a team: Poisson arrivals, an exponential wait before work starts, lognormal cycle times, never-started and not-planned issues, bots, abandoned and unreviewed PRs, lognormal PR stages, all cut off at *as of* the way an ingest sees them. The tests check:

- **exactly**, against brute-force loops over the same items: percentiles per type and group, histogram counts, every week's throughput, every day's WIP and cumulative flow, the aging list, and PR stage medians and bottleneck;
- **statistically**, on a year at high volume: cycle-time p50/p85/p95 within 10% of the lognormal's true quantiles, throughput within 5% of the arrival rates, mean WIP within 5% of Little's law (L = λW), PR stage medians within 10%, and the right bottleneck;
- **edge cases** by hand: no items, all items in progress, items finished the day they started, weeks with zero throughput, window boundaries, excluded items, abandoned PRs, never-started issues, starts dated after finishes, reviews after the merge.

Forecasts are tested in [tests/test_forecast.py](../tests/test_forecast.py):

- **exactly**: constant throughput leaves nothing to chance, so 40 items at 5 a week take exactly 8 weeks in every trial, 8 weeks deliver exactly 40, and a 20% drop gives exactly 10 weeks and 32 items;
- **against enumeration**: for tiny histories, every possible sequence of weeks is listed, and the simulated distributions match the exact probabilities within 1%;
- **by definition**: the 85% date is the first one 85% of trials finished by, the 85% count is the highest one 85% of trials reached, and p50 ≤ p85 ≤ p95 for dates (reversed for counts);
- **for leaks**: changing every week after an origin leaves that origin's backtest forecast unchanged;
- **for calibration**: on 800 weeks of stationary data, the 85% forecast with a long history holds 85% ± 5% of the time, and a 6-week history is measurably overconfident;
- **for speed and repeatability**: 10,000 trials run in well under a second, and a fixed seed repeats exactly.
