"""Failure reporting: one line per cause, not per occurrence."""

import typer

from okpos_cli.cli import _safety_exit, summarize_adaptive_throttle, summarize_failures
from okpos_cli.safety import AccessBlocked
from okpos_cli.throttle import HumanThrottle


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


def test_adaptive_throttle_summary_reports_measured_state():
    throttle = HumanThrottle(max_rps=15)
    assert summarize_adaptive_throttle(throttle) is None

    for _ in range(10):
        throttle.observe_latency(0.2)
    for _ in range(2):
        throttle.observe_latency(2.0)

    summary = summarize_adaptive_throttle(throttle)
    assert summary is not None
    assert "기준선 0.200초" in summary
    assert "적응 감속 1회" in summary


def test_safety_exit_uses_distinct_exit_code(capsys):
    throttle = HumanThrottle(max_requests=10)
    throttle.request_count = 3

    try:
        _safety_exit(AccessBlocked("HTTP 403"), throttle)
    except typer.Exit as exc:
        assert exc.exit_code == 2
    else:
        raise AssertionError("expected typer.Exit")

    output = capsys.readouterr().out
    assert "안전 중단" in output
    assert "3건 / 예산 10건" in output
