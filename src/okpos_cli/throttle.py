"""Request pacing: a hard RPS ceiling plus human-looking jitter.

Three mechanisms stack:

1. A sequential minimum interval enforces the hard ceiling (default 15 req/s).
   It is the contract with the server and is never skipped.
2. On top of that, a lognormal delay imitates a person clicking through
   screens, with an occasional longer pause as if reading a page. This only
   ever *adds* delay, so it cannot breach the ceiling.
3. Response latency establishes a per-run baseline. If its EWMA rises above
   3x baseline, an adaptive delay is added and later removed gradually.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import numpy as np

from .safety import RequestBudgetExceeded

ADAPTIVE_WARMUP_SAMPLES = 10
ADAPTIVE_EWMA_ALPHA = 0.2
ADAPTIVE_SLOW_RATIO = 3.0
ADAPTIVE_RECOVER_RATIO = 1.5
ADAPTIVE_RECOVERY_FACTOR = 0.8
ADAPTIVE_MAX_DELAY_SECONDS = 5.0
ADAPTIVE_MIN_DELAY_SECONDS = 0.01
ADAPTIVE_BASELINE_CEILING_SECONDS = 0.3

_ResultT = TypeVar("_ResultT")


class HumanThrottle:
    """Sequential rate limiter with jitter and latency-adaptive slowdown."""

    def __init__(
        self,
        max_rps: float = 15.0,
        *,
        jitter_median: float = 0.18,
        jitter_sigma: float = 0.55,
        pause_probability: float = 0.04,
        pause_range: tuple[float, float] = (1.2, 3.5),
        max_requests: int | None = None,
        seed: int | None = None,
    ) -> None:
        if max_rps <= 0:
            raise ValueError("max_rps must be positive")
        if max_requests is not None and max_requests <= 0:
            raise ValueError("max_requests must be positive")
        self.max_rps = max_rps
        self.max_requests = max_requests
        self._min_interval = 1.0 / max_rps
        self._jitter_mu = float(np.log(jitter_median))
        self._jitter_sigma = jitter_sigma
        self._pause_probability = pause_probability
        self._pause_range = pause_range
        self._rng = np.random.default_rng(seed)
        # Guards _next_allowed so parallel callers cannot both slip through.
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self.request_times: list[float] = []
        self.request_count = 0
        self._warmup_latencies: list[float] = []
        self.response_count = 0
        self.latency_baseline_seconds: float | None = None
        self.latency_ewma_seconds: float | None = None
        self.adaptive_delay_seconds = 0.0
        self.max_adaptive_delay_seconds = 0.0
        self.adaptive_events = 0

    def _draw_delay(self) -> float:
        """Sample the extra human-like delay for one request."""
        jitter = float(self._rng.lognormal(self._jitter_mu, self._jitter_sigma))
        # Clamp the lognormal tail so a single draw cannot stall the run.
        jitter = min(jitter, 2.0)
        if self._rng.random() < self._pause_probability:
            jitter += float(self._rng.uniform(*self._pause_range))
        return jitter

    def wait(self) -> None:
        """Block until the next request is allowed to go out."""
        with self._lock:
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise RequestBudgetExceeded(
                    f"HTTP request budget exhausted ({self.request_count}/{self.max_requests})"
                )
            self.request_count += 1
            now = time.monotonic()
            start = max(now, self._next_allowed)
            delay = self._draw_delay()
            # The ceiling is the floor of the gap. Jitter and adaptive slowdown
            # only push the next request later, never above max_rps.
            self._next_allowed = (
                start
                + max(self._min_interval, delay)
                + self.adaptive_delay_seconds
            )
            sleep_for = start - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        self.request_times.append(time.monotonic())

    def run_request(self, request: Callable[[], _ResultT]) -> _ResultT:
        """Pace and time one synchronous HTTP request."""
        self.wait()
        started = time.monotonic()
        result = request()
        self.observe_latency(time.monotonic() - started)
        return result

    def observe_latency(self, seconds: float) -> None:
        """Update the healthy baseline, latency EWMA, and extra request gap.

        The first ten responses establish a median baseline. Once warm, an
        EWMA above 3x baseline adds a bounded delay; sustained recovery below
        1.5x removes that delay gradually instead of snapping back at once.
        """
        if not math.isfinite(seconds) or seconds < 0:
            return

        with self._lock:
            self.response_count += 1
            if self.latency_baseline_seconds is None:
                self._warmup_latencies.append(seconds)
                if len(self._warmup_latencies) < ADAPTIVE_WARMUP_SAMPLES:
                    return
                baseline = statistics.median(self._warmup_latencies)
                # Monotonic clocks can yield zero in mocked or extremely fast
                # calls; a tiny floor keeps the ratio defined.
                self.latency_baseline_seconds = min(
                    max(baseline, 1e-6), ADAPTIVE_BASELINE_CEILING_SECONDS
                )
                self.latency_ewma_seconds = self.latency_baseline_seconds
                return

            baseline = self.latency_baseline_seconds
            current = self.latency_ewma_seconds or baseline
            ewma = (
                ADAPTIVE_EWMA_ALPHA * seconds
                + (1.0 - ADAPTIVE_EWMA_ALPHA) * current
            )
            self.latency_ewma_seconds = ewma
            ratio = ewma / baseline
            previous_delay = self.adaptive_delay_seconds

            if ratio >= ADAPTIVE_SLOW_RATIO:
                target = min(ewma - baseline, ADAPTIVE_MAX_DELAY_SECONDS)
                self.adaptive_delay_seconds = max(previous_delay, target)
                if previous_delay == 0.0 and self.adaptive_delay_seconds > 0.0:
                    self.adaptive_events += 1
            elif ratio <= ADAPTIVE_RECOVER_RATIO and previous_delay > 0.0:
                recovered = previous_delay * ADAPTIVE_RECOVERY_FACTOR
                self.adaptive_delay_seconds = (
                    0.0 if recovered < ADAPTIVE_MIN_DELAY_SECONDS else recovered
                )

            self.max_adaptive_delay_seconds = max(
                self.max_adaptive_delay_seconds, self.adaptive_delay_seconds
            )
            if self.adaptive_delay_seconds > 0.0:
                # A slow response may already have outlived the gap scheduled
                # by wait(). Make the newly learned delay affect the very next
                # request, not the one after it.
                self._next_allowed = max(
                    self._next_allowed,
                    time.monotonic() + self.adaptive_delay_seconds,
                )

    def observed_peak_rps(self, window: float = 1.0) -> float:
        """Highest request count seen in any `window`-second span."""
        times = self.request_times
        if len(times) < 2:
            return float(len(times))
        peak, left = 0, 0
        for right in range(len(times)):
            while times[right] - times[left] > window:
                left += 1
            peak = max(peak, right - left + 1)
        return float(peak)
