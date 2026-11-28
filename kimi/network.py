"""Shared retry primitives for Kimi-DLP HTTP operations."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any


RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    retries: int = 2
    base_delay: float = 0.35
    max_delay: float = 10.0
    jitter: float = 0.15
    statuses: frozenset[int] = field(default_factory=lambda: RETRYABLE_STATUS_CODES)

    def __post_init__(self) -> None:
        object.__setattr__(self, "retries", max(0, min(int(self.retries), 10)))
        object.__setattr__(self, "base_delay", max(0.0, float(self.base_delay)))
        object.__setattr__(self, "max_delay", max(0.0, float(self.max_delay)))
        object.__setattr__(self, "jitter", max(0.0, float(self.jitter)))

    def should_retry(self, status_code: int) -> bool:
        return int(status_code) in self.statuses

    def delay(self, response: Any = None, attempt: int = 0) -> float:
        retry_after = response if isinstance(response, str) else None
        if response is not None and not isinstance(response, str):
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                value = float(retry_after)
            except (TypeError, ValueError):
                try:
                    parsed = parsedate_to_datetime(str(retry_after))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    value = parsed.timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    value = -1.0
            if value >= 0:
                return min(self.max_delay, value)
        exponential = min(self.max_delay, self.base_delay * (2 ** max(0, int(attempt))))
        if self.jitter:
            exponential += random.uniform(0.0, min(self.jitter, self.max_delay - exponential))
        return min(self.max_delay, max(0.0, exponential))


__all__ = ["RetryPolicy", "RETRYABLE_STATUS_CODES"]
