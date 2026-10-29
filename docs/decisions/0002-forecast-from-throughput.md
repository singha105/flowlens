# 2. Forecast from item counts, and backtest every forecast

- Status: accepted
- Date: 2026-10-29 (decided on Day 4, written down on Day 7)

## Context

Product managers get asked "when will it ship?" and answer from story points converted to weeks in a spreadsheet, padded by instinct. GitHub has no story points, and points are relative to one team at one time, so they don't survive a re-estimate or a new team member. What GitHub does record exactly is when each issue closed and each PR merged.

## Options

- **Story-point velocity.** Not available on GitHub, and the PRD rules out estimation.
- **A trend line through weekly throughput.** One answer with no stated confidence, and it extrapolates whatever the last few weeks did.
- **Monte Carlo over weekly throughput.** Resample past weeks' delivered-item counts thousands of times and read percentiles off the simulated futures. Assumes the future resembles the sampled past and that items are of similar size.

## Decision

Forecast with Monte Carlo over weekly item counts: by default the last 12 complete weeks, 10,000 trials, one unit at a time (issues or PRs), answered at 50%, 85% and 95%, plus what a 20% throughput drop would do. Ship a backtest beside it: from each of the last 52 weeks, re-make the forecast with only the weeks before and record how often each percentile held. The method and its assumptions are in [METRICS.md](../METRICS.md#forecasting).

## Consequences

- Forecasts state a confidence that can be checked. On streamlit/streamlit the check failed: over 52 origins, 85% issue forecasts four weeks out held 71% of the time and the 95% held 85%. The summary therefore tells users to quote the 95%, instead of hiding the miss.
- Rising throughput makes resampling pessimistic on counts and optimistic on confidence; the method has no trend term. 26 weeks of history calibrated better for streamlit's issues (79% and 83% held), but the default stays 12 weeks, as the brief set it, and is a flag away.
- Item size is ignored. Splitting work differently changes the forecast.
- A repo that delivers a handful of items a month gets forecasts that hold trivially, so below 10 items per horizon the summary doesn't judge calibration.
