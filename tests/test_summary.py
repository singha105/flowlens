"""The executive summary: each judgment follows a stated rule over the numbers."""

import pytest

from flowlens.summary import dora_tiles, executive_summary


def flow(
    *,
    pr_p85: float = 8.1,
    prs: int = 884,
    bottleneck: str | None = "waiting_for_review",
    reviewed: float = 0.27,
) -> dict:
    cycle = {"count": prs, "p50": 1.1, "p85": pr_p85, "p95": 38.0}
    stages = {
        "coding": {"count": prs, "median_hours": 0.01},
        "waiting_for_review": {"count": 612, "median_hours": 28.1},
        "in_review": {"count": 609, "median_hours": 0.0},
        "waiting_to_merge": {"count": 609, "median_hours": 3.6},
    }
    return {
        "cycle_time_days": {
            "pr": {"all": cycle},
            "issue": {"all": {"count": 266, "p50": 4.0, "p85": 150.0, "p95": 471.6}},
        },
        "pr_flow": {
            "merged": prs,
            "stages": stages,
            "bottleneck": bottleneck,
            "bottleneck_share": 0.89 if bottleneck else None,
            "monthly": [
                {"month": "2026-09", "reviewed_share": 0.6},
                {"month": "2026-10", "reviewed_share": reviewed},
            ],
        },
    }


def dora(
    *, per_week=0.54, freq_band="medium", waiting=0.6, rate=0.5, cfr_band="low", deploys=14
) -> dict:
    return {
        "deployment_source": "releases",
        "bands": {},
        "metrics": {
            "deployment_frequency": {"per_week": per_week, "band": freq_band, "deploys": deploys,
                                     "median_gap_days": 12.9},
            "lead_time": {"waiting_share": waiting, "band": "medium", "changes": 864,
                          "p50_days": 12.5, "p85_days": 27.7, "coding_p50_days": 1.3,
                          "waiting_p50_days": 8.4},
            "change_failure_rate": {"deploys": deploys, "failed": round(rate * deploys),
                                    "rate": rate if deploys else None, "band": cfr_band,
                                    "failure_window_hours": 72.0},
            "recovery_time": {"failed": 7, "recovered": 6, "open": 1, "median_hours": 357.6,
                              "band": "low"},
        },
    }  # fmt: skip


def backtest(held85: float, held95: float = 0.85, delivered: float = 52.5) -> dict:
    return {
        "unit": "issue",
        "scores": [
            {"horizon_weeks": 8, "history_weeks": 12, "held_85": 0.1, "held_95": 0.1,
             "mean_actual": 103.8},
            {"horizon_weeks": 4, "history_weeks": 12, "held_85": held85, "held_95": held95,
             "mean_actual": delivered},
        ],
    }  # fmt: skip


def test_the_demo_repo_summary_reads_like_the_brief():
    bullets = [str(b) for b in executive_summary(flow(), dora(), backtest(0.71))]

    assert bullets == [
        "PRs are predictable, forecasts optimistic: 85% of PRs merge within 8.1 days of "
        "starting, but 85% issue forecasts held only 71% of the time in backtests, so quote "
        "the 95% (held 85%).",
        "The bottleneck is waiting, not building: waiting for first review takes a median of "
        "28 hours, 89% of the summed PR stage medians. Human reviews are getting rare: 27% of "
        "October's merged PRs had one.",
        "DORA: medium deployment frequency (0.54 a week), but 60% of lead time is waiting for "
        "a release; 50% of deploys failed within 72 hours (low band).",
    ]


@pytest.mark.parametrize(
    ("held85", "lead"),
    [
        (0.79, "PRs are predictable, forecasts optimistic"),
        (0.80, "Delivery is predictable"),  # within 5 points of 85% is close enough
        (0.90, "Delivery is predictable"),
        (0.91, "Delivery is predictable, forecasts cautious"),
    ],
)
def test_forecasts_are_judged_by_how_often_the_85_percent_held(held85, lead):
    (delivery, *_) = executive_summary(flow(), dora(), backtest(held85))

    assert delivery.lead == lead


def test_without_a_backtest_the_summary_says_so():
    (delivery, *_) = executive_summary(flow(), dora(), None)

    assert delivery.text == (
        "85% of PRs merge within 8.1 days of starting; there's too little history to backtest "
        "forecasts."
    )


def test_a_trickle_of_deliveries_is_too_little_to_judge_forecasts():
    (delivery, *_) = executive_summary(flow(), dora(), backtest(1.0, 1.0, delivered=0.6))

    assert delivery.lead == "Delivery"
    assert delivery.text.endswith(
        "; too few issues are delivered (0.6 per 4 weeks) to judge forecasts."
    )


def test_issues_stand_in_when_no_prs_were_delivered():
    (delivery, *_) = executive_summary(flow(prs=0), dora(), backtest(0.84))

    assert delivery.text.startswith("85% of issues close within 150 days of starting")


@pytest.mark.parametrize(
    ("stage", "lead"),
    [
        ("waiting_to_merge", "The bottleneck is waiting, not building"),
        ("coding", "The bottleneck is building"),
        ("in_review", "The bottleneck is review itself"),
    ],
)
def test_the_bottleneck_lead_names_the_kind_of_stage(stage, lead):
    (_, bottleneck, _) = executive_summary(flow(bottleneck=stage, reviewed=0.9), dora(), None)

    assert bottleneck.lead == lead
    assert "Human reviews" not in bottleneck.text  # 90% reviewed is not worth a sentence


def test_reviews_are_rare_unless_an_earlier_month_had_them():
    report = flow()
    for month in report["pr_flow"]["monthly"]:
        month["reviewed_share"] = 0.3

    (_, bottleneck, _) = executive_summary(report, dora(), None)

    assert bottleneck.text.endswith("Human reviews are rare: 30% of October's merged PRs had one.")


def test_without_human_reviews_there_is_no_review_wait_to_measure():
    report = flow(bottleneck="coding", reviewed=0.0)
    for stage in ("waiting_for_review", "in_review", "waiting_to_merge"):
        report["pr_flow"]["stages"][stage] = {"count": 0, "median_hours": None}

    (_, bottleneck, _) = executive_summary(report, dora(), None)

    assert str(bottleneck) == (
        "PR flow: none of the 884 merged PRs had a human review, so there's no review wait to "
        "measure."
    )


def test_dora_says_and_or_but_by_how_much_lead_time_is_waiting():
    *_, mostly_coding = executive_summary(flow(), dora(waiting=0.3, freq_band=None), None)
    *_, no_deploys = executive_summary(flow(), dora(deploys=0), None)

    assert mostly_coding.text == (
        "deployment frequency (0.54 a week), and 30% of lead time is waiting for a release; "
        "50% of deploys failed within 72 hours (low band)."
    )
    assert no_deploys.text == "no deploys from releases in the window."


def test_one_or_two_deploys_are_too_few_to_rate():
    *_, one_deploy = executive_summary(flow(), dora(deploys=1, rate=0.0, cfr_band="elite"), None)

    assert one_deploy.text.endswith(
        "; 0 of 1 deploys failed within 72 hours, too few deploys to rate."
    )
    assert "elite" not in one_deploy.text


def test_dora_tiles_carry_their_band():
    tiles = dora_tiles(dora())

    assert [(t.label, t.value, t.band) for t in tiles] == [
        ("Deployment frequency", "0.54 a week", "medium"),
        ("Lead time for changes (p50)", "12.5 days", "medium"),
        ("Change failure rate", "50%", "low"),
        ("Recovery time (median)", "14.9 days", "low"),
    ]
    assert tiles[3].note == "6 of 7 failed deploys recovered, 1 still open"
