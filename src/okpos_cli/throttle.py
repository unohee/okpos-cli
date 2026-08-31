"""Request pacing: a hard RPS ceiling plus human-looking jitter.

Two mechanisms stack:

1. A sequential minimum interval enforces the hard ceiling (default 15 req/s).
   It is the contract with the server and is never skipped.
2. On top of that, a lognormal delay imitates a person clicking through
   screens, with an occasional longer pause as if reading a page. This only
   ever *adds* delay, so it cannot breach the ceiling.
"""

from __future__ import annotations

import threading
import time

import numpy as np


class HumanThrottle:
    """Sequential rate limiter with lognormal human-like jitter."""

    def __init__(
        self,
        max_rps: float = 15.0,
        *,
        jitter_median: float = 0.18,
        jitter_sigma: float = 0.55,
        pause_probability: float = 0.04,
        pause_range: tuple[float, float] = (1.2, 3.5),
        seed: int | None = None,
    ) -> None:
        if max_rps <= 0:
            raise ValueError("max_rps must be positive")
        self.max_rps = max_rps
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
            now = time.monotonic()
            start = max(now, self._next_allowed)
            delay = self._draw_delay()
            # The ceiling is the floor of the gap; jitter only pushes it later.
            self._next_allowed = start + max(self._min_interval, delay)
            sleep_for = start - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        self.request_times.append(time.monotonic())

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
