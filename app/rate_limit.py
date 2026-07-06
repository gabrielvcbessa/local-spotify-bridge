from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SpotifyRateLimiter:
    window_seconds: float = 30.0
    soft_requests_per_window: int = 20
    soft_ratio: float = 0.8
    backoff_multiplier: float = 1.25
    max_poll_interval_seconds: float = 60.0
    retry_after_padding_seconds: float = 0.5
    _requests: deque[float] = field(default_factory=deque)
    _blocked_until: float = 0.0
    _last_retry_after_seconds: float | None = None
    _last_429_at: float | None = None

    def record_request(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._prune(now)
        self._requests.append(now)

    def record_response(
        self,
        status_code: int,
        retry_after: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        self._prune(now)
        if status_code != 429:
            return

        retry_after_seconds = self._parse_retry_after(retry_after)
        self._last_retry_after_seconds = retry_after_seconds
        self._last_429_at = now
        self._blocked_until = max(
            self._blocked_until,
            now + retry_after_seconds + self.retry_after_padding_seconds,
        )

    def wait_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._prune(now)
        return max(0.0, self._blocked_until - now)

    def poll_interval(self, base_interval_seconds: float, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._prune(now)
        interval = max(0.0, base_interval_seconds)
        interval = max(interval, self.wait_seconds(now))

        soft_limit = max(1, self.soft_requests_per_window)
        threshold = max(1, int(soft_limit * self.soft_ratio))
        request_count = len(self._requests)
        if request_count >= threshold:
            pressure = (request_count - threshold + 1) / max(1, soft_limit - threshold + 1)
            multiplier = self.backoff_multiplier + max(0.0, pressure - 1.0)
            interval = max(interval, base_interval_seconds * multiplier)

        return min(interval, self.max_poll_interval_seconds)

    def status(self, base_poll_interval_seconds: float, now: float | None = None) -> dict[str, object]:
        now = time.monotonic() if now is None else now
        self._prune(now)
        soft_limit = max(1, self.soft_requests_per_window)
        threshold = max(1, int(soft_limit * self.soft_ratio))
        retry_after_remaining = self.wait_seconds(now)
        return {
            "window_seconds": self.window_seconds,
            "soft_requests_per_window": soft_limit,
            "soft_threshold_requests": threshold,
            "requests_in_window": len(self._requests),
            "near_threshold": len(self._requests) >= threshold,
            "retry_after_remaining_seconds": round(retry_after_remaining, 3),
            "last_retry_after_seconds": self._last_retry_after_seconds,
            "has_seen_429": self._last_429_at is not None,
            "min_poll_interval_seconds": base_poll_interval_seconds,
            "adaptive_poll_interval_seconds": round(
                self.poll_interval(base_poll_interval_seconds, now),
                3,
            ),
        }

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._requests and self._requests[0] < cutoff:
            self._requests.popleft()
        if self._blocked_until <= now:
            self._blocked_until = 0.0

    @staticmethod
    def _parse_retry_after(value: str | None) -> float:
        if value is None:
            return 1.0
        try:
            return max(0.0, float(value))
        except ValueError:
            return 1.0
