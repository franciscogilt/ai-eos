"""Per-provider circuit breaker.

closed -> (N consecutive failures) -> open -> (reset period) -> half_open
half_open: one trial call; success closes, failure re-opens.
State is per process; each worker protects itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

State = Literal["closed", "open", "half_open"]


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_after_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _trial_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> State:
        if self._opened_at is None:
            return "closed"
        if self.clock() - self._opened_at >= self.reset_after_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._trial_in_flight = False
        if self._opened_at is not None:  # failed trial: open again for a full period
            self._opened_at = self.clock()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self.clock()
