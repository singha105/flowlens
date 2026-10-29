# FlowLens on a resume

Every number below was measured in this project. Its source is in the table at the end, so it can be checked before it's quoted.

## Bullet options

Pick one per resume, by the role.

**1. Software or data engineering**

> Built FlowLens, an open-source delivery analytics tool (Python, DuckDB, Streamlit, Plotly) that computes Kanban flow metrics, Monte Carlo forecasts and DORA metrics from GitHub history; its incremental GraphQL ingest loaded 5,716 issues and PRs in 7.3 minutes on 6% of the hourly API budget, and 417 offline tests check the metrics against synthetic ground truth.

Shorter: *Built FlowLens (Python, DuckDB, Streamlit), an open-source GitHub delivery analytics tool; ingested 5,716 issues and PRs in 7.3 minutes on 6% of the API budget, backed by 417 offline tests.*

**2. DevOps, SRE or platform**

> Built FlowLens, which computes traceable DORA metrics by mapping 4,208 merged PRs to the release that shipped them via GitHub's compare API (117 through cherry-picks), and publishes a weekly report to GitHub Pages from a $0 GitHub Actions pipeline; on streamlit/streamlit it found median PR lead time fell from 22.2 to 9.3 days after releases went from monthly to fortnightly.

Shorter: *Built a $0 GitHub Actions pipeline that publishes weekly DORA and flow metrics to GitHub Pages; mapped 4,208 PRs to the releases that shipped them.*

**3. Product, analytics or AI-adjacent roles**

> Built FlowLens, an open-source delivery analytics tool computing Kanban flow, Monte Carlo forecasts and DORA metrics from GitHub data; backtested across 52 weeks of streamlit/streamlit history, it showed 85% forecasts held only 71% of the time, so its generated summary tells teams to quote the 95% forecast (held 85%).

Shorter: *Built FlowLens, a GitHub delivery analytics tool whose 52-week backtest showed 85% forecasts held 71% of the time, and changed its guidance to the 95% forecast (held 85%).*

## Interview pitch

FlowLens is an open-source tool I built that turns a GitHub repository's history into flow metrics, Monte Carlo delivery forecasts and DORA metrics, running locally for free with every number traceable to the issues and PRs behind it. What I'm proudest of is that it checks itself: on streamlit's own history it showed that a forecast promising 85% confidence held only 71% of the time, so the report tells you to quote the 95% date instead.

## Where each number comes from

| Number | Source |
|---|---|
| 5,716 issues and PRs in 7.3 minutes, GraphQL cost 318 of 5,000 (6%) | First one-year streamlit ingest, [PROGRESS Day 2](PROGRESS.md) |
| 417 offline tests | `uv run pytest`, Day 7 |
| 4,208 PRs matched to the release that shipped them, 117 through cherry-picks | [PROGRESS Day 5](PROGRESS.md) |
| 85% forecast held 71% (issues) and 79% (PRs); 95% held 85% and 92%; 52 weekly origins | `flowlens backtest`, [INSIGHTS.md](INSIGHTS.md#3-forecasts-promised-85-and-delivered-71-quote-the-95-date) |
| Median PR lead time 22.2 days (monthly releases) and 9.3 days (fortnightly) | Per-release split of `dora_streamlit__streamlit.json`, [INSIGHTS.md](INSIGHTS.md#1-release-cadence-not-coding-decides-how-long-a-change-takes-to-reach-users) |
| Waiting for a first review is 89% of the summed PR stage medians (28 hours) | `flowlens flow`, [INSIGHTS.md](INSIGHTS.md#2-the-wait-for-a-first-review-was-the-bottleneck-and-human-review-is-now-the-exception) |
| GitHub dropped timeline events for 52 of 100 issues on pages of 50; fixed by paging at 25 | [PROGRESS Day 2](PROGRESS.md) |
| $0 to run: GitHub Actions and Pages free tier, the built-in `GITHUB_TOKEN` | [decision 0004](decisions/0004-local-first-zero-cost.md) |

Keep the wording honest: FlowLens has no users or adoption numbers to cite. It's a portfolio project, and the findings describe one public repository.
