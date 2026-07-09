from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Lock
from time import time
from typing import Any


DEFAULT_PERIODS_SECONDS: dict[str, int] = {
    "1h": 3600,
    "3h": 3 * 3600,
    "6h": 6 * 3600,
    "12h": 12 * 3600,
    "1d": 24 * 3600,
    "3d": 3 * 24 * 3600,
    "7d": 7 * 24 * 3600,
}


@dataclass(frozen=True)
class TelemetryEvent:
    ts: float
    kind: str
    request_type: str
    target: str
    status: str
    status_code: int | None = None
    latency_ms: float | None = None
    payload_bytes: int | None = None
    retain: bool | None = None
    qos: int | None = None
    wait_seconds: float | None = None
    retry_after: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["at"] = datetime.fromtimestamp(self.ts, UTC).isoformat()
        return payload


class BridgeTelemetry:
    def __init__(self, *, max_events: int = 50_000, retention_seconds: float = 7 * 24 * 3600) -> None:
        self._events: deque[TelemetryEvent] = deque(maxlen=max_events)
        self._retention_seconds = retention_seconds
        self._lock = Lock()

    def configure(self, *, max_events: int, retention_seconds: float) -> None:
        with self._lock:
            existing = list(self._events)[-max_events:]
            self._events = deque(existing, maxlen=max_events)
            self._retention_seconds = retention_seconds
            self._prune_locked(time())

    def record_spotify_api(
        self,
        *,
        method: str,
        path: str,
        status_code: int | None,
        latency_ms: float | None,
        wait_seconds: float,
        retry_after: str | None,
        error: str | None = None,
    ) -> None:
        status = "ok" if status_code is not None and 200 <= status_code < 400 and error is None else "error"
        self._append(
            TelemetryEvent(
                ts=time(),
                kind="spotify_api_request",
                request_type=f"{method.upper()} {path}",
                target=path,
                status=status,
                status_code=status_code,
                latency_ms=latency_ms,
                wait_seconds=wait_seconds,
                retry_after=retry_after,
                error=error,
            )
        )

    def record_mqtt_publish(
        self,
        *,
        topic: str,
        payload_kind: str,
        payload_bytes: int,
        retain: bool,
        qos: int,
        published: bool,
        skipped_reason: str | None = None,
    ) -> None:
        self._append(
            TelemetryEvent(
                ts=time(),
                kind="mqtt_posting",
                request_type=topic,
                target=topic,
                status="published" if published else "skipped",
                payload_bytes=payload_bytes,
                retain=retain,
                qos=qos,
                error=skipped_reason,
            )
        )

    def snapshot(
        self,
        *,
        periods_seconds: dict[str, int] | None = None,
        recent_limit: int = 100,
    ) -> dict[str, Any]:
        periods = periods_seconds or DEFAULT_PERIODS_SECONDS
        now = time()
        with self._lock:
            self._prune_locked(now)
            events = list(self._events)

        return {
            "ok": True,
            "generated_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "retention_seconds": self._retention_seconds,
            "stored_events": len(events),
            "periods": {
                label: self._period_summary(events, now - seconds)
                for label, seconds in periods.items()
            },
            "recent": [event.to_dict() for event in events[-recent_limit:]][::-1],
        }

    def _append(self, event: TelemetryEvent) -> None:
        now = time()
        with self._lock:
            self._prune_locked(now)
            self._events.append(event)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._retention_seconds
        while self._events and self._events[0].ts < cutoff:
            self._events.popleft()

    def _period_summary(self, events: list[TelemetryEvent], cutoff: float) -> dict[str, Any]:
        filtered = [event for event in events if event.ts >= cutoff]
        spotify = self._aggregate(filtered, kind="spotify_api_request")
        mqtt = self._aggregate(filtered, kind="mqtt_posting")
        return {
            "total_events": len(filtered),
            "spotify_api_requests": spotify,
            "mqtt_postings": mqtt,
        }

    def _aggregate(self, events: list[TelemetryEvent], *, kind: str) -> dict[str, Any]:
        by_type: dict[str, dict[str, Any]] = {}
        total = 0
        failures = 0
        skipped = 0
        for event in events:
            if event.kind != kind:
                continue
            total += 1
            bucket = by_type.setdefault(
                event.request_type,
                {
                    "count": 0,
                    "ok": 0,
                    "errors": 0,
                    "skipped": 0,
                    "avg_latency_ms": None,
                    "last_at": None,
                    "last_status": None,
                    "last_status_code": None,
                    "last_error": None,
                    "_last_ts": 0.0,
                    "_latency_total": 0.0,
                    "_latency_count": 0,
                },
            )
            bucket["count"] += 1
            if event.status in {"ok", "published"}:
                bucket["ok"] += 1
            elif event.status == "skipped":
                bucket["skipped"] += 1
                skipped += 1
            else:
                bucket["errors"] += 1
                failures += 1
            if event.latency_ms is not None:
                bucket["_latency_total"] += event.latency_ms
                bucket["_latency_count"] += 1
            if bucket["last_at"] is None or event.ts >= bucket["_last_ts"]:
                bucket["_last_ts"] = event.ts
                bucket["last_at"] = datetime.fromtimestamp(event.ts, UTC).isoformat()
                bucket["last_status"] = event.status
                bucket["last_status_code"] = event.status_code
                bucket["last_error"] = event.error

        for bucket in by_type.values():
            latency_count = bucket.pop("_latency_count")
            latency_total = bucket.pop("_latency_total")
            bucket.pop("_last_ts", None)
            if latency_count:
                bucket["avg_latency_ms"] = round(latency_total / latency_count, 2)

        return {
            "total": total,
            "ok": total - failures - skipped,
            "errors": failures,
            "skipped": skipped,
            "by_type": dict(sorted(by_type.items())),
        }


telemetry = BridgeTelemetry()
