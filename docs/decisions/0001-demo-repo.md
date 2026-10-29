# 1. Demo repository for the case study

- Status: accepted
- Date: 2026-10-29

## Context

Day 7 ends with a case study: FlowLens run against one public repository, with its flow metrics, forecasts, backtests, and DORA numbers written up. The choice affects every day before that, because the metrics get tuned against the data the case study will show.

The repository needs to be:

- busy enough that weekly throughput and percentiles mean something: the most closed issues and merged PRs over the last 12 months;
- carrying a clear deployment signal, releases or GitHub Deployments, so DORA has something to count;
- public, active this month, and not a toy.

Day 1's smoke-test repo, pallets/markupsafe, fails the first test: 384 PRs in its whole history, half of them from bots.

## Options

Three active public repos with different working styles, measured on 2026-10-29 over the 12 months since 2025-10-29 with `scripts/compare_repos.py`. Volumes come from GitHub search. The shares come from each repo's 100 most recently updated closed issues and merged PRs, fetched and modelled exactly as `flowlens ingest` does it, with the default settings.

| | cli/cli | streamlit/streamlit | pydantic/pydantic |
|---|---:|---:|---:|
| Issues closed | 1,091 | 783 | 348 |
| of which completed | 337 | 711 | 209 |
| PRs merged | 661 | **3,300** | 616 |
| Closed + merged | 1,752 | **4,083** | 964 |
| Stable releases | 30 | 23 | 12 |
| Deployments (latest 100) | production 86, gh-acceptance-testing 14 | nightly 90, release 10 | release 100 |
| Merged PRs from bots | 25% | 57% | 10% |
| Human PRs with a human review | 100% | 44% | 17% |
| Human PRs approved | 100% | 42% | 14% |
| Closed issues not planned | 67% | 2% | 15% |
| Finished issues with a start | 26% | 86% | 88% |
| ... by assignment | 6% | 61% | 58% |
| ... by linked PR | 19% | 25% | 31% |

What the numbers say:

- **cli/cli** closes the most issues, but two thirds of them as not planned: it's a triage queue as much as a delivery record, and only a quarter of finished issues show any sign of work starting. Every human PR gets reviewed and approved, and its `production` Deployments are the cleanest deploy signal of the three.
- **streamlit/streamlit** merges five times as many PRs as either of the others, and 86% of its recently closed issues have a start signal. More than half of its merged PRs come from bots, though (release automation, dependency updates, AI-assisted chores), and fewer than half of the human ones get a review from someone else.
- **pydantic/pydantic** has issue coverage as good as streamlit's but a quarter of the volume, and only 17% of human PRs in the sample were reviewed by someone other than the author.

Bots don't decide it: with every bot PR removed, streamlit still has about 1,400 human PRs merged plus 711 issues completed (the full ingest below counts 1,680), against about 500 and 337 for cli/cli.

A first, rougher version of this comparison fetched 100 issues per page and reported that only 18% of streamlit's closed issues were assigned and only 6% of close events carried a reason. Both were wrong: GitHub silently drops timeline events from big issue pages. FlowLens now reads issues 25 to a page; see Day 2 in [PROGRESS.md](../PROGRESS.md).

## Decision

Use **streamlit/streamlit** for the case study. It has the most closed issues and merged PRs by a wide margin, with or without bots, a high share of issues with a start signal, and a release train that gives a clear deployment signal.

Its settings live in the `repos: streamlit/streamlit:` section of `flowlens.yaml`.

## Consequences

- **Bots are excluded by default** (`exclude.bots: true`), and the quality report says how much that removes. For streamlit that's the single biggest adjustment; throughput with bots included would mostly measure automation. This answers PRD open question 2.
- **Review metrics need splitting in time.** Until mid-2026 every human PR got a human review. From July a bot (github-actions running an AI review) started approving PRs, and by October only 30% of merged human PRs had a human review while 78% had a bot approval. FlowLens never counts bot reviews, so time to first review and review coverage change meaning partway through the year; the case study has to show the before and after rather than one annual number.
- **Deploys come from releases.** Streamlit's GitHub Deployments go to `nightly` (a pre-release build every night) and `release` (which mirrors the release itself), so neither is production and the default `deployment_environments: [production, prod]` finds none. Releases arrive about twice a month, so deployment frequency and change failure rate rest on roughly 23 points a year; the report flags that. The `Build Release` workflow is ingested too, as a second signal. Its failed runs are the closest thing to a failed deploy, and its successful runs build exactly the commit each release tags.
- **Issue starts come mostly from linked PRs.** Over the year, 48% of finished issues started with a linked PR and 19% with an assignment, while 33% never started; assignment only leads in the latest month, which is why the recent-items sample above looks different. GitHub reports `willCloseTarget: false` once the closing PR merges, so the PR named as the closer of the `ClosedEvent` has to count as a link (it does).
- **No incident label.** Streamlit labels bugs `type:bug` and regressions `type:regression`; there is no incident or hotfix label. `type:regression` stands in for incidents until Day 5 defines change failure rate, and may need replacing with reverts or patch releases.
- **Label names contain colons.** `type:bug` is fine unquoted in YAML, but GitHub's common `type: bug` (with a space) is not; config validation explains how to quote it.
- **cli/cli stays useful for tests.** It has the only manually connected issue and the clearest "changes requested" review in the fixtures, and its `production` Deployments are a good second case for the `deployments` source later.

## Ingest

The first 12-month ingest, on 2026-10-29 (`flowlens ingest --repo streamlit/streamlit --since 2025-10-29`):

- 1,340 issues, 4,376 PRs, 23 releases, 332 deployments, and 35 `Build Release` runs, in 233 API calls (GraphQL cost 318 of 5,000) and 7.3 minutes. A re-run straight after took 6 calls and 8 seconds.
- 20 random issues and 20 random PRs matched fresh single-item fetches exactly (timeline events and reviews).

```
Work items: 5,716 | with started_at: 83% | closed not planned (excluded): 0%
  1,340 issues, 4,376 PRs; 2,045 excluded (bot authors 1,954, duplicates 48, not planned 24, label:status:cannot-reproduce 13, label:type:no-streamlit-issue 6)
Issues: 1,248 counted | with started_at: 51% (first_linked_pr 33%, first_assigned 17%)
  closed without ever starting: 237 of 726 finished (33%) | no closing reason: 0 of 815 closed (0%)
PRs: 2,423 counted, 1,680 merged | with a review: 83% | median time to first review: 26h
  approved before merge: 83% | merged with no human review: 287
Deployments (releases): 23 in 12 months (1.9 a month)
  signals found: releases 23 · deployments 332 (nightly 300, release 32) · workflow 'Build Release' 35 runs
```
