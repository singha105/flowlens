# 3. DORA from a configurable deploy source, with bands from cited reports only

- Status: accepted
- Date: 2026-10-29 (decided on Days 2 and 5, written down on Day 7)

## Context

DORA's four keys need deploys, and most public repos don't record production deploys. Streamlit's GitHub Deployments go to `nightly` and `release` environments, neither of them production (see [0001](0001-demo-repo.md)). Its failures aren't labelled as incidents either: there is no incident or hotfix label, only `type:regression`. And the 2025 DORA report replaced the elite/high/medium/low levels with archetypes, so there is no current official cut-off to compare against.

## Decision

- The deploy source is a setting, `deployment_source` in `flowlens.yaml`: releases (the default), GitHub Deployments to named production environments, or a named workflow's runs.
- Each merged PR is matched to the first deploy whose compare range carries it, by merge commit or by a commit naming it, so cherry-picks ship with the patch release.
- A deploy failed if a revert of a PR it shipped, a hotfix or incident label, or a failed deployment status lands within `dora.failure_window_hours` (72 by default).
- Performance bands live only in `flowlens.yaml`, each with the report it comes from. FlowLens has none built in, and prints none without them.

## Consequences

- Every DORA number is traceable: the JSON lists each deploy with the PRs it shipped and the signals against it, and a CSV lists each failure and why it counted.
- For a library, a release means "available on PyPI", not "running in production"; the report says which source it used.
- The change failure rate is a statement about the window as much as the releases: streamlit's is 21% with 24 hours, 50% with 72, 79% with 7 days. The bullet always quotes the rate with its window.
- Bands are approximate, from the 2024 and 2023 reports' clusters, and a user can replace them with their own targets.
