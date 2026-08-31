"""The RPS ceiling is a contract with the server, so it gets a real test."""

import time

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
