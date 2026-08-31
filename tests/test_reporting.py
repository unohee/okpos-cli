"""Failure reporting: one line per cause, not per occurrence."""

from okpos_cli.cli import summarize_failures


def test_same_cause_across_shops_collapses_to_one_line():
    # This is the real shape: one broken screen, repeated for every shop.
    failures = [
        ("sale.cust.day_sale020#1@2026-08-29", "code=-9 미등록 SQL Index 번호입니다.")
        for _ in range(16)
    ]
    summary = summarize_failures(failures)
    assert len(summary) == 1
    name, _msg, count = summary[0]
    assert name == "sale.cust.day_sale020#1"
    assert count == 16


def test_distinct_causes_stay_separate_and_rank_by_count():
    failures = (
        [("a#1@d", "boom")] * 3
        + [("b#1@d", "bang")] * 7
        + [("c#1@d", "pop")]
    )
    summary = summarize_failures(failures)
    assert [(n, c) for n, _m, c in summary] == [("b#1", 7), ("a#1", 3), ("c#1", 1)]


def test_same_screen_with_different_messages_is_not_merged():
    failures = [("a#1@d", "first"), ("a#1@d", "second")]
    assert len(summarize_failures(failures)) == 2


def test_dates_do_not_split_a_group():
    # The same screen failing across a date range is still one cause.
    failures = [(f"a#1@2026-08-{d:02d}", "same") for d in range(1, 31)]
    summary = summarize_failures(failures)
    assert len(summary) == 1 and summary[0][2] == 30


def test_limit_caps_the_output():
    failures = [(f"s{i}#1@d", f"m{i}") for i in range(25)]
    assert len(summarize_failures(failures, limit=10)) == 10


def test_no_failures_yields_nothing():
    assert summarize_failures([]) == []
