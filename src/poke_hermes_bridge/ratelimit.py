"""Simple in-memory token-bucket rate limiter, keyed per API key."""

import time


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def allow(self, key: str) -> tuple[bool, float]:
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        if self.per_minute <= 0:
            return True, 0.0
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.per_minute), now))
        tokens = min(float(self.per_minute), tokens + (now - last) * self.per_minute / 60.0)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        self._buckets[key] = (tokens, now)
        retry = (1.0 - tokens) * 60.0 / self.per_minute
        return False, max(retry, 0.1)
