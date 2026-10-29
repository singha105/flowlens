# FlowLens — Product Requirements

Status: Draft v0.1 · Owner: Arnab Singh · Last updated: 2026-10-29

## Problem

Teams that work on GitHub get asked two questions every week: "how are we doing?" and "when will it be done?" The answers usually come from gut feel, a velocity chart nobody trusts, or a paid analytics product that needs org-wide access and a security review. Meanwhile the evidence is already in the repo. Every issue, pull request, review, release, and deployment has a timestamp. Nobody turns that history into flow metrics because doing it by hand means scraping APIs, cleaning data, and arguing about definitions.

FlowLens reads a repository's history and produces flow metrics, probabilistic delivery forecasts, and DORA metrics. Every definition is written down, and every number can be traced back to the issues and PRs behind it.

## Target users

**Engineering manager (primary).** Runs a team of 4–10 engineers working out of one or a few GitHub repos. Wants to see where work stalls (PRs waiting days for a first review, issues open for weeks with no PR) and needs numbers to bring to a retro or a staffing conversation without turning them into a leaderboard. Cares that definitions are explicit and that the tool is free and runs locally, because getting a new vendor approved takes a quarter.

**Product manager.** Owns a roadmap and gets asked for dates. Today they convert story points to weeks in a spreadsheet and pad the result. They want a forecast stated as a range with a confidence level ("85% chance these 30 items are done by Dec 12"), an honest view of how often past forecasts would have been right, and a report they can link from a planning doc without asking engineering for access.

## Jobs to be done

- When I'm asked "when will this ship?", I want a date range with a stated confidence, so I can commit without padding by instinct.
- When delivery feels slow, I want to see where time goes between first commit, first review, and merge, so I fix the actual bottleneck.
- When I report on delivery health, I want DORA metrics computed from what actually happened in GitHub, so the numbers hold up when someone asks how they were made.
- When I share findings, I want a static report I can link, so readers don't have to install anything.

## Goals and non-goals

Goals for v1:

1. Ingest a GitHub repo with the user's own `gh` credentials, incrementally, inside the free API rate limits.
2. Compute flow metrics, Monte Carlo forecasts, and DORA metrics with written definitions and per-item drill-down.
3. Backtest forecasts against the repo's own history so users can see how far to trust them.
4. Ship a local Streamlit dashboard and a static HTML report that can be published to GitHub Pages.
5. Cost $0 to run: no hosted service, no paid API, data stays on the user's machine.

Non-goals for v1:

- Ranking or scoring individual developers. FlowLens reports on the system of work, not on people.
- Jira, Linear, or GitLab as data sources. The data model should leave room for them later.
- Real-time updates, webhooks, or a hosted multi-tenant service.
- Story-point estimation. Forecasts use item counts only.

## Metrics

| Metric | Definition |
|---|---|
| Cycle time | Time from work starting (first commit on the PR, else PR opened) to the work finishing (PR merged, or linked issue closed as completed). |
| Lead time | Time from an issue being created to it closing as completed. |
| Throughput | Items finished per week: issues closed as completed, PRs merged. |
| WIP | Items started but not finished at a given point in time. |
| Aging WIP | Age of each in-progress item, compared with historical cycle-time percentiles. |
| PR flow breakdown | A PR's life split into time to first review and review to merge, alongside size (lines added + deleted). |
| Monte Carlo "when" | Distribution of completion dates for N items, from resampling historical weekly throughput. |
| Monte Carlo "how many" | Distribution of items finished by a target date. |
| Forecast backtest | How often a forecast made at a past date, at a given percentile, covered what actually happened. |
| Deployment frequency | Successful production deployments per week (GitHub Deployments, else releases). |
| Lead time for changes | Time from a PR's first commit to the deployment that shipped it. |
| Change failure rate | Share of deployments followed within 7 days by a revert, a hotfix, or an incident-labelled issue. |
| Failed deployment recovery time | Time from a failure signal to the next successful deployment or the incident closing. |

Durations are reported as 50th/85th/95th percentiles rather than means, because cycle-time distributions are heavily skewed.

## Success criteria for v1

- A first ingest of a repo with about 5,000 issues and PRs finishes in under 10 minutes; a re-run finishes in under 1 minute; neither uses more than 20% of the hourly GraphQL budget.
- Ingestion is idempotent: running it twice leaves the same rows and no duplicates.
- Every metric links back to the items it came from, and a spot check of 20 items matches what GitHub's UI shows.
- On the case-study repo, the 85th-percentile "when" forecast covers the actual outcome in 75–95% of backtest windows.
- The dashboard renders 10,000 items in under 3 seconds, and CI publishes the HTML report to GitHub Pages.
- A new user goes from `git clone` to a report in under 15 minutes with only `gh` and `uv` installed.

## Risks and assumptions

- **GitHub is a lossy record of work.** There is no "in progress" state. We assume the first commit or the PR opening approximates the start; teams with long-lived local branches or design work outside GitHub will look faster than they are.
- **Issue–PR links are incomplete.** Issue cycle time depends on closing keywords and linked PRs. Unlinked work falls back to issue timestamps, and the report must say how much of it there is.
- **DORA needs a deploy signal.** Many repos never use GitHub Deployments. Releases, tags, and workflow runs on the default branch are weaker proxies, and the report must state which one it used.
- **Forecasts assume the future resembles the past.** Team changes, holidays, and scope growth break that. Backtesting makes the error visible; it doesn't remove it.
- **Rate limits.** Large repos can exhaust the 5,000-point hourly GraphQL budget, so ingestion has to back off, resume, and fetch incrementally.
- **Metrics invite gaming.** Published numbers tend to become targets. Defaults show team-level aggregates and keep individual names out of headline charts.

## Open questions

1. What starts the clock for an issue with no linked PR: assignment, a label, or creation?
2. Should bot-authored PRs (Dependabot, Renovate) be excluded by default or shown as their own series?
3. For reopened items, do we measure to the first close or the last?
4. Calendar days or business days for cycle time?
5. Which deploy signal wins when a repo has both releases and Deployments?
6. Which public repo makes the best case study: active, consistently labelled, and with real deployments?
