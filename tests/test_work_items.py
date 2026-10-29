from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from fakes import fixture_items
from flowlens.config import Config, load_config, parse_config
from flowlens.ingest.github import issue_event_rows, issue_row, pull_request_row, review_rows
from flowlens.model.work_items import (
    classify,
    derive_start,
    exclusion_reason,
    issue_work_item,
    linked_prs,
    pull_request_work_item,
)

REPO = "acme/shop"
STREAMLIT = "streamlit/streamlit"
STREAMLIT_CONFIG = load_config(Path(__file__).parent.parent / "flowlens.yaml", STREAMLIT)
DEFAULT = Config()

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
_ids = count(1)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


# --- hand-built rows, shaped like storage returns them ----------------------------------


DONE = day(10)


def issue(*, state="CLOSED", closed=DONE, reason="COMPLETED", labels=(), author_type="User"):
    return {
        "number": 1,
        "title": "Checkout fails",
        "url": "https://github.com/acme/shop/issues/1",
        "state": state,
        "state_reason": reason,
        "created_at": T0,
        "closed_at": closed if state == "CLOSED" else None,
        "author": "ann",
        "author_type": author_type,
        "labels": list(labels),
    }


def event(event_type, at, **fields):
    return {"id": f"E{next(_ids)}", "event_type": event_type, "created_at": at, **fields}


def assigned(at, who="bo"):
    return event("assigned", at, assignee=who, actor="ann")


def labeled(at, label):
    return event("labeled", at, label=label, actor="ann")


def pr_link(at, opened, number=7, repo=REPO, kind="cross_referenced", will_close=True):
    return event(
        kind, at, pr_number=number, pr_repo=repo, pr_created_at=opened, will_close=will_close
    )


def closed(at, reason="COMPLETED", **fields):
    return event("closed", at, state_reason=reason, **fields)


def start_of(row, events, config=DEFAULT, **kwargs):
    item, _ = issue_work_item(row, events, config, repo=REPO, **kwargs)
    return item.started_at, item.started_signal


# --- derive_start -----------------------------------------------------------------------


def test_earliest_signal_wins():
    assert derive_start([("a", day(3)), ("b", day(1)), ("c", None)], done_at=day(9)) == (
        day(1),
        "b",
    )


def test_ties_go_to_the_signal_listed_first():
    assert derive_start([("a", day(1)), ("b", day(1))], done_at=None) == (day(1), "a")
    assert derive_start([("b", day(1)), ("a", day(1))], done_at=None) == (day(1), "b")


def test_signals_after_done_are_ignored():
    assert derive_start([("a", day(5)), ("b", day(2))], done_at=day(4)) == (day(2), "b")
    assert derive_start([("a", day(5))], done_at=day(4)) == (None, None)
    assert derive_start([("a", day(4))], done_at=day(4)) == (day(4), "a")  # same instant counts


def test_start_is_clamped_to_the_floor():
    assert derive_start([("a", day(-2))], done_at=None, floor=T0) == (T0, "a")


def test_no_signal_means_no_start():
    assert derive_start([], done_at=None) == (None, None)
    assert derive_start([("a", None)], done_at=day(1)) == (None, None)


# --- issue start rules ------------------------------------------------------------------


def test_first_assignment_starts_an_issue():
    events = [assigned(day(2)), event("unassigned", day(3), assignee="bo"), assigned(day(4))]

    assert start_of(issue(), events) == (day(2), "first_assigned")


def test_a_linked_pr_starts_the_issue_when_it_was_opened_not_when_linked():
    events = [pr_link(day(5), opened=day(3))]

    assert start_of(issue(), events) == (day(3), "first_linked_pr")


def test_the_earliest_of_several_linked_prs_counts():
    events = [pr_link(day(5), opened=day(4), number=7), pr_link(day(6), opened=day(2), number=8)]

    item, _ = issue_work_item(issue(), events, DEFAULT, repo=REPO)

    assert (item.started_at, item.linked) == (day(2), [7, 8])


@pytest.mark.parametrize("label", ["in progress", "In Progress", "IN PROGRESS"])
def test_the_in_progress_label_starts_an_issue_case_insensitively(label):
    events = [labeled(day(1), "bug"), labeled(day(3), label)]

    assert start_of(issue(), events) == (day(3), "label:in progress")


def test_a_start_label_counts_even_if_removed_later():
    events = [labeled(day(3), "in progress"), event("unlabeled", day(4), label="in progress")]

    assert start_of(issue(), events) == (day(3), "label:in progress")


def test_a_plain_mention_is_not_a_link():
    events = [pr_link(day(2), opened=day(1), will_close=False)]

    item, item_events = issue_work_item(issue(), events, DEFAULT, repo=REPO)

    assert (item.started_at, item.linked) == (None, [])
    assert [e.kind for e in item_events] == ["created"]  # the mention isn't kept as an event


def test_a_pr_in_another_repo_is_not_a_link():
    events = [pr_link(day(2), opened=day(1), repo="acme/docs")]

    assert start_of(issue(), events) == (None, None)


def test_the_pr_that_closed_the_issue_is_a_link():
    # GitHub reports willCloseTarget=false once a PR merges, so the closer is the evidence.
    events = [
        pr_link(day(2), opened=day(1), will_close=False),
        closed(day(6), pr_number=7, pr_repo=REPO, pr_created_at=day(1)),
    ]

    item, _ = issue_work_item(issue(closed=day(6)), events, DEFAULT, repo=REPO)

    assert (item.started_at, item.started_signal, item.linked) == (day(1), "first_linked_pr", [7])


def test_a_manually_connected_pr_is_a_link():
    events = [pr_link(day(3), opened=day(2), kind="connected", will_close=None)]

    item, item_events = issue_work_item(issue(), events, DEFAULT, repo=REPO)

    assert item.started_at == day(2)
    assert [(e.kind, e.detail) for e in item_events][-1] == ("linked_pr", "#7")


def test_a_pr_listing_the_issue_in_its_closing_references_is_a_link():
    item, _ = issue_work_item(issue(), [], DEFAULT, repo=REPO, closing_prs={9: day(4)})

    assert (item.started_at, item.linked) == (day(4), [9])


def test_a_pr_opened_before_the_issue_starts_it_when_it_was_filed():
    events = [pr_link(day(1), opened=day(-3))]

    assert start_of(issue(), events) == (T0, "first_linked_pr")


def test_config_decides_which_signals_count():
    events = [assigned(day(1)), labeled(day(2), "doing")]
    config = parse_config({"started_signal": ["label:doing"]})

    assert start_of(issue(), events, config) == (day(2), "label:doing")
    assert start_of(issue(), events) == (day(1), "first_assigned")


def test_config_order_breaks_ties():
    events = [assigned(day(2)), pr_link(day(2), opened=day(2))]
    pr_first = parse_config({"started_signal": ["first_linked_pr", "first_assigned"]})

    assert start_of(issue(), events)[1] == "first_assigned"
    assert start_of(issue(), events, pr_first)[1] == "first_linked_pr"


# --- closed without ever starting, and reopened issues ----------------------------------


def test_an_issue_closed_without_any_signal_never_started():
    item, item_events = issue_work_item(issue(), [closed(day(10))], DEFAULT, repo=REPO)

    assert (item.started_at, item.done_at, item.outcome) == (None, day(10), "completed")
    assert [e.kind for e in item_events] == ["created", "closed"]


def test_triage_after_closing_does_not_start_an_issue():
    events = [closed(day(10)), assigned(day(11)), labeled(day(12), "in progress")]

    assert start_of(issue(), events) == (None, None)


def test_a_reopened_issue_finishes_at_its_last_close():
    events = [
        closed(day(2)),
        event("reopened", day(3)),
        assigned(day(4)),  # work began only after it was reopened
        closed(day(10)),
    ]

    item, item_events = issue_work_item(issue(closed=day(10)), events, DEFAULT, repo=REPO)

    assert (item.started_at, item.done_at, item.reopen_count) == (day(4), day(10), 1)
    assert [e.kind for e in item_events] == ["created", "closed", "reopened", "assigned", "closed"]


def test_a_signal_before_the_first_close_still_counts_after_a_reopen():
    events = [assigned(day(1)), closed(day(2)), event("reopened", day(3)), closed(day(10))]

    assert start_of(issue(closed=day(10)), events) == (day(1), "first_assigned")


def test_a_signal_after_the_final_close_is_ignored_even_when_reopened_before():
    events = [closed(day(2)), event("reopened", day(3)), closed(day(5)), assigned(day(6))]

    assert start_of(issue(closed=day(5)), events) == (None, None)


def test_a_reopened_issue_that_is_still_open_has_no_finish():
    events = [closed(day(2)), event("reopened", day(3)), assigned(day(4))]
    row = issue(state="OPEN", reason="REOPENED")

    item, _ = issue_work_item(row, events, DEFAULT, repo=REPO)

    assert (item.done_at, item.outcome, item.started_at, item.reopen_count) == (
        None,
        "open",
        day(4),
        1,
    )
    assert item.excluded is False  # REOPENED is a state, not a reason to drop the issue


# --- outcomes and exclusions ------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "outcome", "excluded"),
    [
        ("COMPLETED", "completed", None),
        ("NOT_PLANNED", "not_planned", "state_reason:NOT_PLANNED"),
        ("DUPLICATE", "duplicate", "state_reason:DUPLICATE"),
        (None, "closed_no_reason", None),
    ],
)
def test_issue_outcomes(reason, outcome, excluded):
    item, _ = issue_work_item(issue(reason=reason), [], DEFAULT, repo=REPO)

    assert (item.outcome, item.exclude_reason, item.excluded) == (
        outcome,
        excluded,
        excluded is not None,
    )


def test_exclusion_by_label_and_bot_author():
    def reason(**kwargs):
        defaults = {"state_reason": None, "labels": [], "author_type": "User", "config": DEFAULT}
        return exclusion_reason(**(defaults | kwargs))

    assert reason(labels=["Duplicate"]) == "label:duplicate"
    assert reason(author_type="Bot") == "bot"
    assert reason(author_type="Bot", config=parse_config({"exclude": {"bots": False}})) is None
    assert reason(state_reason="not_planned", labels=["wontfix"]) == "state_reason:NOT_PLANNED"
    assert reason(labels=["bug"]) is None


def test_classify_reads_the_configured_labels():
    assert classify(["Bug", "sev1"], DEFAULT) == ["bug", "incident"]
    assert classify(["type:bug", "type:regression"], STREAMLIT_CONFIG) == ["bug", "incident"]
    assert classify(["bug"], STREAMLIT_CONFIG) == []


def test_linked_prs_keeps_the_first_linking_event():
    first = pr_link(day(2), opened=day(1))
    again = closed(day(5), pr_number=7, pr_repo=REPO, pr_created_at=day(1))

    assert linked_prs([first, again], REPO) == {7: (day(1), first)}


# --- recorded streamlit and cli issues --------------------------------------------------


ISSUES = fixture_items("streamlit_issues.json")


def recorded(number, nodes=ISSUES, repo=STREAMLIT, config=STREAMLIT_CONFIG):
    node = nodes[number]
    return issue_work_item(issue_row(node), issue_event_rows(node), config, repo=repo)


def test_recorded_issue_completed_without_ever_starting():
    item, _ = recorded(17733)

    assert (item.started_at, item.outcome) == (None, "completed")
    assert item.kinds == ["bug", "incident"]  # type:bug and type:regression


def test_recorded_issue_assigned_then_unassigned_still_started():
    item, item_events = recorded(16726)

    assert (item.started_at, item.started_signal) == (utc(2026, 8, 30, 15, 11, 5), "first_assigned")
    assert item.done_at is None
    assert "unassigned" in [e.kind for e in item_events]


def test_recorded_issue_closed_by_a_pr_without_assignment():
    item, _ = recorded(11752)

    assert (item.started_at, item.started_signal) == (
        utc(2026, 8, 8, 12, 20, 26),
        "first_linked_pr",
    )
    assert item.linked == [16418]


def test_recorded_issue_assigned_before_its_pr_opened():
    item, item_events = recorded(17665)

    assert (item.started_at, item.started_signal) == (
        utc(2026, 10, 26, 18, 21, 35),
        "first_assigned",
    )
    assert item.linked == [17671]
    # The PR's earlier cross-reference had no closing keyword: a mention, not kept.
    assert "linked_pr" not in [e.kind for e in item_events]


def test_recorded_issue_referenced_only_from_other_repos():
    item, _ = recorded(12801)

    assert (item.started_at, item.linked) == (None, [])


def test_recorded_issue_closed_reopened_closed_by_a_pr():
    item, _ = recorded(8793)

    assert item.reopen_count == 1
    assert item.done_at == utc(2026, 8, 3, 16, 3, 49)
    assert item.started_at == utc(2026, 8, 1, 16, 14, 58)  # PR 16286, opened two years later
    assert item.created_at == utc(2024, 5, 29, 18, 18, 24)


def test_recorded_issue_reopened_and_still_open():
    item, _ = recorded(8271)

    assert (item.outcome, item.done_at, item.started_at, item.excluded) == (
        "open",
        None,
        None,
        False,
    )
    assert item.reopen_count == 1


def test_recorded_issue_relabelled_from_bug():
    item, _ = recorded(14555)

    assert item.kinds == []  # classified by its labels now, not its history
    assert item.started_at is None  # a PR mentioned it, nobody claimed it


def test_recorded_issue_closed_not_planned_never_started():
    item, _ = recorded(14209)

    assert (item.outcome, item.excluded, item.exclude_reason, item.started_at) == (
        "not_planned",
        True,
        "state_reason:NOT_PLANNED",
        None,
    )


def test_recorded_issue_open_and_assigned():
    item, _ = recorded(10023)

    assert (item.started_at, item.done_at, item.outcome) == (
        utc(2025, 5, 12, 19, 41, 55),
        None,
        "open",
    )


def test_recorded_issue_manually_connected_to_a_pr():
    item, item_events = recorded(14614, fixture_items("cli_issues.json"), "cli/cli", DEFAULT)

    # Assigned to Copilot seven seconds before its PR opened.
    assert (item.started_at, item.started_signal) == (utc(2026, 10, 7, 8, 24, 31), "first_assigned")
    assert item.linked == [14616]
    assert ("linked_pr", "#14616") in [(e.kind, e.detail) for e in item_events]
    assert item.kinds == ["bug"]


# --- pull requests ----------------------------------------------------------------------


PRS = fixture_items("streamlit_prs.json")


def recorded_pr(number, config=STREAMLIT_CONFIG):
    node = PRS[number]
    return pull_request_work_item(pull_request_row(node, STREAMLIT), review_rows(node), config)


def test_merged_pr_starts_at_its_first_commit():
    item, item_events = recorded_pr(17730)

    assert (item.started_at, item.started_signal) == (utc(2026, 10, 28, 20, 40, 13), "first_commit")
    assert (item.done_at, item.outcome) == (utc(2026, 10, 28, 21, 1, 14), "merged")
    kinds = [(e.kind, e.actor, e.detail) for e in item_events]
    assert kinds[:3] == [
        ("created", "lukasmasuch", None),
        ("first_commit", None, None),
        ("review_commented", "copilot-pull-request-reviewer", "bot"),
    ]
    assert ("review_approved", "sfc-gh-lmasuch", None) in kinds
    assert ("review_approved", "github-actions", "bot") in kinds
    assert kinds[-1] == ("merged", None, "95e2aa71230cd09c7736a98f4e258933e8fd54b0")


def test_closed_unmerged_pr_is_abandoned():
    item, item_events = recorded_pr(17670)

    assert (item.outcome, item.done_at) == ("abandoned", utc(2026, 10, 28, 17, 12, 14))
    assert item.started_at == utc(2026, 10, 23, 16, 46, 59)  # three days before it opened
    assert item_events[-1].kind == "closed"


def test_open_pr_has_no_finish():
    item, _ = recorded_pr(17704)

    assert (item.outcome, item.done_at) == ("open", None)


def test_bot_pr_is_excluded():
    item, _ = recorded_pr(17735)

    assert (item.excluded, item.exclude_reason) == (True, "bot")


def test_pr_links_the_issues_it_closes():
    item, _ = recorded_pr(17688)

    assert item.linked == [17612]


def test_pr_start_signal_is_configurable():
    item, _ = recorded_pr(17730, parse_config({"pr_started_signal": ["opened"]}))

    assert (item.started_at, item.started_signal) == (utc(2026, 10, 28, 20, 40, 36), "opened")


def test_pr_without_commits_starts_when_opened():
    row = pull_request_row(PRS[17730], STREAMLIT) | {"first_commit_at": None}

    item, _ = pull_request_work_item(row, [], DEFAULT)

    assert item.started_signal == "opened"
