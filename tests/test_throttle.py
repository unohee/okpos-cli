"""The RPS ceiling is a contract with the server, so it gets a real test."""

import time

import pytest

from okpos_cli.safety import RequestBudgetExceeded
from okpos_cli.throttle import HumanThrottle


def test_ceiling_is_never_breached():
    throttle = HumanThrottle(max_rps=15, seed=1)
    for _ in range(45):
        throttle.wait()
    assert throttle.observed_peak_rps(window=1.0) <= 15


def test_jitter_varies_between_requests():
    throttle = HumanThrottle(max_rps=15, seed=3)
    delays = {round(throttle._draw_delay(), 6) for _ in range(50)}
    # A constant sleep would collapse to a single value.
    assert len(delays) > 40


def test_minimum_interval_is_respected():
    throttle = HumanThrottle(max_rps=5, seed=9)
    start = time.monotonic()
    for _ in range(6):
        throttle.wait()
    # 6 requests at <=5 rps cannot finish faster than 5 intervals.
    assert time.monotonic() - start >= 5 * (1 / 5) * 0.9


def test_rejects_non_positive_rate():
    try:
        HumanThrottle(max_rps=0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for max_rps=0")


def test_request_budget_is_a_hard_ceiling(monkeypatch):
    throttle = HumanThrottle(max_rps=15, max_requests=2)
    monkeypatch.setattr(throttle, "_draw_delay", lambda: 0.0)
    monkeypatch.setattr("okpos_cli.throttle.time.sleep", lambda _seconds: None)

    throttle.wait()
    throttle.wait()
    with pytest.raises(RequestBudgetExceeded, match="2/2"):
        throttle.wait()

    assert throttle.request_count == 2


def test_rejects_non_positive_request_budget():
    with pytest.raises(ValueError, match="max_requests"):
        HumanThrottle(max_requests=0)


def test_latency_baseline_uses_warmup_median():
    throttle = HumanThrottle(max_rps=15)
    for latency in [0.18] * 9 + [1.8]:
        throttle.observe_latency(latency)

    assert throttle.response_count == 10
    assert throttle.latency_baseline_seconds == pytest.approx(0.18)
    assert throttle.latency_ewma_seconds == pytest.approx(0.18)
    assert throttle.adaptive_delay_seconds == 0.0


def test_slow_warmup_cannot_normalize_an_overloaded_server():
    throttle = HumanThrottle(max_rps=15)
    for _ in range(10):
        throttle.observe_latency(1.0)

    assert throttle.latency_baseline_seconds == pytest.approx(0.3)
    for _ in range(20):
        throttle.observe_latency(1.0)

    assert throttle.adaptive_events == 1
    assert throttle.adaptive_delay_seconds > 0.0


def test_subthreshold_latency_does_not_slow_requests():
    throttle = HumanThrottle(max_rps=15)
    for _ in range(10):
        throttle.observe_latency(0.2)
    for _ in range(100):
        throttle.observe_latency(0.59)  # settles below the 3x trigger

    assert throttle.latency_ewma_seconds == pytest.approx(0.59, rel=1e-6)
    assert throttle.adaptive_events == 0
    assert throttle.adaptive_delay_seconds == 0.0


def test_slowdown_is_bounded_and_recovers_gradually():
    throttle = HumanThrottle(max_rps=15)
    for _ in range(10):
        throttle.observe_latency(0.2)
    for _ in range(20):
        throttle.observe_latency(20.0)

    assert throttle.adaptive_events == 1
    assert throttle.adaptive_delay_seconds == 5.0
    assert throttle.max_adaptive_delay_seconds == 5.0

    first_reduction = None
    for _ in range(200):
        previous = throttle.adaptive_delay_seconds
        throttle.observe_latency(0.2)
        if 0.0 < throttle.adaptive_delay_seconds < previous:
            first_reduction = throttle.adaptive_delay_seconds
            break

    assert first_reduction is not None  # recovery starts without snapping to zero
    for _ in range(200):
        throttle.observe_latency(0.2)
    assert throttle.adaptive_delay_seconds == 0.0
    assert throttle.adaptive_events == 1


def test_run_request_records_round_trip_latency(monkeypatch):
    throttle = HumanThrottle(max_rps=15)
    monkeypatch.setattr(throttle, "wait", lambda: None)
    moments = iter([10.0, 10.25])
    monkeypatch.setattr("okpos_cli.throttle.time.monotonic", lambda: next(moments))

    assert throttle.run_request(lambda: "ok") == "ok"
    assert throttle.response_count == 1
