"""Minimal circuit breaker for optional dependencies (Redis).

After a failure the circuit opens for ``reset_after_seconds``; callers skip the dependency
and use their fallback immediately instead of waiting for a timeout on every request. After
the cooldown one call is allowed through again; success closes the circuit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

CircuitState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        reset_after_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._reset_after = reset_after_seconds
        self._clock = clock
        self._opened_at: float | None = None
        self.failures = 0

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._reset_after:
            return "half_open"
        return "open"

    @property
    def degraded(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        self._opened_at = self._clock()
