import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket

from .config import Settings
from .models import PlaybackSnapshot, StateEnvelope

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - dependency is present in package installs
    mqtt = None


def states_are_meaningfully_different(
    previous: PlaybackSnapshot | None,
    current: PlaybackSnapshot | None,
    *,
    progress_drift_ms: int,
) -> bool:
    if previous is None or current is None:
        return previous is not current

    comparable_fields = (
        "is_playing",
        "item_id",
        "item_uri",
        "title",
        "device_id",
        "device_is_active",
        "device_volume_percent",
        "volume_control_supported",
        "shuffle_state",
        "repeat_state",
        "next_track",
        "previous_track",
    )
    for field in comparable_fields:
        if getattr(previous, field) != getattr(current, field):
            return True

    if previous.progress_ms is None or current.progress_ms is None:
        return previous.progress_ms != current.progress_ms

    return abs(previous.progress_ms - current.progress_ms) > progress_drift_ms


class ConnectionBroker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._websockets: set[WebSocket] = set()
        self._mqtt_client = None
        self._mqtt_loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_snapshot_factory: Callable[[int, PlaybackSnapshot | None], Awaitable[dict[str, Any]]] | None = None
        self._mqtt_config_factory: Callable[[], dict[str, Any]] | None = None
        self._mqtt_command_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
        self._mqtt_request_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
        self._version = 0
        self.current_state: PlaybackSnapshot | None = None
        self.last_poll_at: str | None = None
        self.last_spotify_error: str | None = None
        self.last_mqtt_availability: dict[str, Any] | None = None
        self.last_mqtt_availability_at: str | None = None
        self._mqtt_payload_fingerprints: dict[str, str] = {}
        self._forward_transition_expected_until = 0.0
        self._lock = asyncio.Lock()

    def set_mqtt_snapshot_factory(
        self,
        factory: Callable[[int, PlaybackSnapshot | None], Awaitable[dict[str, Any]]],
    ) -> None:
        self._mqtt_snapshot_factory = factory

    def set_mqtt_config_factory(self, factory: Callable[[], dict[str, Any]]) -> None:
        self._mqtt_config_factory = factory

    def set_mqtt_command_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
    ) -> None:
        self._mqtt_command_handler = handler

    def set_mqtt_request_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
    ) -> None:
        self._mqtt_request_handler = handler

    async def start(self) -> None:
        if not self._settings.mqtt_enabled:
            return
        if mqtt is None:
            raise RuntimeError("paho-mqtt is required when MQTT_ENABLED=true.")

        self._mqtt_loop = asyncio.get_running_loop()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self._settings.mqtt_username:
            client.username_pw_set(self._settings.mqtt_username, self._settings.mqtt_password or None)
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.connect(self._settings.mqtt_host, self._settings.mqtt_port, 60)
        client.loop_start()
        self._mqtt_client = client
        self._mqtt_payload_fingerprints = {}
        await self.publish_mqtt_config()

    async def stop(self) -> None:
        if self._mqtt_client is not None:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None
            self._mqtt_loop = None

    @property
    def version(self) -> int:
        return self._version

    async def add_websocket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._websockets.add(websocket)
            envelope = StateEnvelope(event="snapshot", state=self.current_state, version=self._version)
        await websocket.send_json(envelope.model_dump(mode="json"))

    async def remove_websocket(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._websockets.discard(websocket)

    async def publish_if_changed(self, new_state: PlaybackSnapshot | None) -> bool:
        self.mark_spotify_success()
        track_changed = playback_track_changed(self.current_state, new_state)
        new_state = enrich_with_previous_track(
            self.current_state,
            new_state,
            forward_transition_expected=self.forward_transition_expected,
        )
        changed = states_are_meaningfully_different(
            self.current_state,
            new_state,
            progress_drift_ms=self._settings.state_change_progress_drift_ms,
        )
        if not changed:
            return False

        if track_changed:
            self.clear_forward_transition_expected()
        self.current_state = new_state
        self._version += 1
        await self.publish("playback.changed", new_state)
        return True

    def mark_forward_transition_expected(self, ttl_seconds: float = 12.0) -> None:
        self._forward_transition_expected_until = time.monotonic() + ttl_seconds

    def clear_forward_transition_expected(self) -> None:
        self._forward_transition_expected_until = 0.0

    @property
    def forward_transition_expected(self) -> bool:
        if self._forward_transition_expected_until <= 0:
            return False
        if time.monotonic() > self._forward_transition_expected_until:
            self.clear_forward_transition_expected()
            return False
        return True

    async def publish_metadata_changed(self) -> None:
        self._version += 1
        await self.publish("metadata.changed", self.current_state)

    def mark_spotify_success(self) -> None:
        self.last_poll_at = datetime.now(UTC).isoformat()
        self.last_spotify_error = None

    def mark_spotify_error(self, exc: Exception) -> None:
        self.last_poll_at = datetime.now(UTC).isoformat()
        self.last_spotify_error = str(exc)

    async def publish(self, event: str, state: PlaybackSnapshot | None) -> None:
        envelope = StateEnvelope(event=event, state=state, version=self._version)
        payload = envelope.model_dump(mode="json")
        text = json.dumps(payload)

        async with self._lock:
            websockets = list(self._websockets)

        stale: list[WebSocket] = []
        for websocket in websockets:
            try:
                await websocket.send_text(text)
            except RuntimeError:
                stale.append(websocket)

        if stale:
            async with self._lock:
                for websocket in stale:
                    self._websockets.discard(websocket)

        if self._mqtt_client is not None:
            topic = f"{self._settings.mqtt_topic_prefix}/playback"
            self._publish_mqtt_json(topic, payload, retain=True)
            if self._mqtt_snapshot_factory is not None:
                snapshot = await self._mqtt_snapshot_factory(self._version, state)
                self._publish_mqtt_json(
                    self.mqtt_topic("state"),
                    retain=True,
                    payload=snapshot,
                )

    async def publish_mqtt_config(self) -> None:
        if self._mqtt_client is None or self._mqtt_config_factory is None:
            return
        self._publish_mqtt_json(self.mqtt_topic("config"), self._mqtt_config_factory(), retain=True)

    def mqtt_topic(self, leaf: str) -> str:
        prefix = self._settings.mqtt_knob_topic_prefix.strip("/")
        device_id = self._settings.mqtt_knob_device_id.strip("/")
        return f"{prefix}/{device_id}/{leaf}"

    def mqtt_topics(self) -> dict[str, str]:
        return {
            "legacy_playback": f"{self._settings.mqtt_topic_prefix}/playback",
            "state": self.mqtt_topic("state"),
            "config": self.mqtt_topic("config"),
            "command": self.mqtt_topic("command"),
            "command_result": self.mqtt_topic("command_result"),
            "availability": self.mqtt_topic("availability"),
            "library_root": self.mqtt_topic("library/root"),
            "library_page": self.mqtt_topic("library/page"),
            "devices": self.mqtt_topic("devices"),
            "status": self.mqtt_topic("status"),
            "request": self.mqtt_topic("request"),
            "request_result": self.mqtt_topic("request_result"),
            "art_current": self.mqtt_topic("art/current/rgb565"),
            "art_next": self.mqtt_topic("art/next/rgb565"),
            "art_previous": self.mqtt_topic("art/previous/rgb565"),
        }

    def _on_mqtt_connect(self, client, _, __, reason_code, ___) -> None:
        if reason_code != 0:
            self.last_spotify_error = f"MQTT connect failed: {reason_code}"
            return
        client.subscribe(self.mqtt_topic("command"), qos=self._settings.mqtt_qos)
        client.subscribe(self.mqtt_topic("request"), qos=self._settings.mqtt_qos)
        client.subscribe(self.mqtt_topic("availability"), qos=self._settings.mqtt_qos)

    def _on_mqtt_message(self, _, __, message) -> None:
        if self._mqtt_loop is None:
            return
        payload = bytes(message.payload).decode("utf-8")
        self._mqtt_loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._handle_mqtt_message(message.topic, payload))
        )

    async def _handle_mqtt_message(self, topic: str, payload: str) -> None:
        if topic == self.mqtt_topic("availability"):
            self.last_mqtt_availability_at = datetime.now(UTC).isoformat()
            try:
                availability = json.loads(payload)
                self.last_mqtt_availability = availability if isinstance(availability, dict) else {"value": availability}
            except json.JSONDecodeError:
                self.last_mqtt_availability = {"value": payload}
            return
        if topic == self.mqtt_topic("request"):
            await self._handle_mqtt_rpc(payload, self._mqtt_request_handler, self.mqtt_topic("request_result"), "request")
            return
        if topic != self.mqtt_topic("command"):
            return

        await self._handle_mqtt_rpc(payload, self._mqtt_command_handler, self.mqtt_topic("command_result"), "command")

    async def _handle_mqtt_rpc(
        self,
        payload: str,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None,
        result_topic: str,
        label: str,
    ) -> None:
        if handler is None:
            return
        try:
            message = json.loads(payload)
            if not isinstance(message, dict):
                raise ValueError(f"MQTT {label} payload must be a JSON object.")
            result = await handler(message)
            response = {
                "ok": True,
                "request_id": message.get("request_id"),
                label: message.get("type"),
            }
            if isinstance(result, dict):
                response.update(result)
            else:
                response["result"] = result
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}

        if self._mqtt_client is not None:
            self._mqtt_client.publish(
                result_topic,
                json.dumps(response),
                qos=self._settings.mqtt_qos,
                retain=False,
            )

    async def publish_mqtt_retained(self, topic_key: str, payload: dict[str, Any]) -> None:
        if self._mqtt_client is None:
            return
        self._publish_mqtt_json(self.mqtt_topic(topic_key), payload, retain=True)

    async def publish_mqtt_retained_bytes(self, topic_key: str, payload: bytes) -> None:
        if self._mqtt_client is None:
            return
        self._publish_mqtt_bytes(self.mqtt_topic(topic_key), payload, retain=True)

    def _publish_mqtt_json(self, topic: str, payload: dict[str, Any], *, retain: bool) -> bool:
        if self._mqtt_client is None:
            return False

        text = json.dumps(payload)
        if retain:
            fingerprint = mqtt_payload_fingerprint(payload)
            if self._mqtt_payload_fingerprints.get(topic) == fingerprint:
                return False
            self._mqtt_payload_fingerprints[topic] = fingerprint

        self._mqtt_client.publish(topic, text, qos=self._settings.mqtt_qos, retain=retain)
        return True

    def _publish_mqtt_bytes(self, topic: str, payload: bytes, *, retain: bool) -> bool:
        if self._mqtt_client is None:
            return False

        if retain:
            fingerprint = f"bytes:{hashlib.sha256(payload).hexdigest()}"
            if self._mqtt_payload_fingerprints.get(topic) == fingerprint:
                return False
            self._mqtt_payload_fingerprints[topic] = fingerprint

        self._mqtt_client.publish(topic, payload, qos=self._settings.mqtt_qos, retain=retain)
        return True


def mqtt_payload_fingerprint(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("payload_hash"), str):
        normalized = strip_mqtt_volatile_fields(payload)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return f"state:{hashlib.sha256(encoded).hexdigest()}"
    if isinstance(payload.get("hash"), str):
        return f"hash:{payload['hash']}"

    normalized = strip_mqtt_volatile_fields(payload)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def strip_mqtt_volatile_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_mqtt_volatile_fields(item)
            for key, item in value.items()
            if key not in {"updated_at_ms", "version", "progress_ms", "payload_hash", "playback_hash"}
        }
    if isinstance(value, list):
        return [strip_mqtt_volatile_fields(item) for item in value]
    return value


def enrich_with_previous_track(
    previous: PlaybackSnapshot | None,
    current: PlaybackSnapshot | None,
    *,
    forward_transition_expected: bool = False,
    track_end_threshold_ms: int = 8_000,
) -> PlaybackSnapshot | None:
    if current is None or previous is None:
        return current

    previous_track = current.previous_track
    if playback_track_changed(previous, current):
        if (
            same_playback_scope(previous, current)
            and current_matches_previous_next_track(previous, current)
            and (forward_transition_expected or playback_ended(previous, threshold_ms=track_end_threshold_ms))
        ):
            previous_track = track_preview_from_snapshot(previous)
        else:
            previous_track = None
    elif previous.previous_track is not None:
        previous_track = previous.previous_track if cached_track_matches_scope(previous.previous_track, current) else None

    if previous_track == current.previous_track:
        return current
    return current.model_copy(update={"previous_track": previous_track})


def playback_track_changed(previous: PlaybackSnapshot | None, current: PlaybackSnapshot | None) -> bool:
    if previous is None or current is None:
        return False
    previous_identity = previous.item_id or previous.item_uri
    current_identity = current.item_id or current.item_uri
    return bool(previous_identity and current_identity and previous_identity != current_identity)


def track_preview_from_snapshot(state: PlaybackSnapshot) -> dict[str, Any] | None:
    if not state.item_id and not state.item_uri and not state.title:
        return None
    context = playback_context(state)
    return {
        "id": state.item_id,
        "uri": state.item_uri,
        "title": state.title,
        "artists": state.artists,
        "artist_text": ", ".join(state.artists),
        "album": state.album,
        "album_art_url": state.album_art_url,
        "album_art_id": state.album_art_id,
        "duration_ms": state.duration_ms,
        "context_type": context["type"],
        "context_uri": context["uri"],
        "album_uri": playback_album_uri(state),
    }


def playback_ended(state: PlaybackSnapshot, *, threshold_ms: int) -> bool:
    if state.progress_ms is None or state.duration_ms is None:
        return False
    return max(state.duration_ms - state.progress_ms, 0) <= threshold_ms


def same_playback_scope(previous: PlaybackSnapshot, current: PlaybackSnapshot) -> bool:
    previous_context_uri = playback_context(previous)["uri"]
    current_context_uri = playback_context(current)["uri"]
    if previous_context_uri or current_context_uri:
        return bool(previous_context_uri and current_context_uri and previous_context_uri == current_context_uri)

    previous_album_uri = playback_album_uri(previous)
    current_album_uri = playback_album_uri(current)
    if previous_album_uri or current_album_uri:
        return bool(previous_album_uri and current_album_uri and previous_album_uri == current_album_uri)

    if previous.album_art_id or current.album_art_id:
        return bool(previous.album_art_id and current.album_art_id and previous.album_art_id == current.album_art_id)

    return bool(previous.album and current.album and previous.album == current.album and previous.artists == current.artists)


def current_matches_previous_next_track(previous: PlaybackSnapshot, current: PlaybackSnapshot) -> bool:
    if previous.next_track is None:
        return False
    next_id = previous.next_track.get("id")
    next_uri = previous.next_track.get("uri")
    if next_id and current.item_id:
        return next_id == current.item_id
    if next_uri and current.item_uri:
        return next_uri == current.item_uri
    return False


def cached_track_matches_scope(cached_track: dict[str, Any], current: PlaybackSnapshot) -> bool:
    cached_context_uri = cached_track.get("context_uri")
    current_context_uri = playback_context(current)["uri"]
    if cached_context_uri or current_context_uri:
        return bool(cached_context_uri and current_context_uri and cached_context_uri == current_context_uri)

    cached_album_uri = cached_track.get("album_uri")
    current_album_uri = playback_album_uri(current)
    if cached_album_uri or current_album_uri:
        return bool(cached_album_uri and current_album_uri and cached_album_uri == current_album_uri)

    cached_album_art_id = cached_track.get("album_art_id")
    if cached_album_art_id or current.album_art_id:
        return bool(cached_album_art_id and current.album_art_id and cached_album_art_id == current.album_art_id)

    return bool(cached_track.get("album") and current.album and cached_track.get("album") == current.album)


def playback_context(state: PlaybackSnapshot) -> dict[str, str | None]:
    raw_context = state.raw.get("context") if isinstance(state.raw, dict) else None
    if not isinstance(raw_context, dict):
        return {"type": None, "uri": None}
    context_type = raw_context.get("type")
    context_uri = raw_context.get("uri")
    return {
        "type": context_type if isinstance(context_type, str) else None,
        "uri": context_uri if isinstance(context_uri, str) else None,
    }


def playback_album_uri(state: PlaybackSnapshot) -> str | None:
    item = state.raw.get("item") if isinstance(state.raw, dict) else None
    if not isinstance(item, dict):
        return None
    album = item.get("album")
    if not isinstance(album, dict):
        return None
    uri = album.get("uri")
    return uri if isinstance(uri, str) else None


class StatePoller:
    def __init__(
        self,
        fetch_state: Callable[[], Awaitable[PlaybackSnapshot | None]],
        broker: ConnectionBroker,
        interval_seconds: float,
        interval_strategy: Callable[[float], float] | None = None,
    ) -> None:
        self._fetch_state = fetch_state
        self._broker = broker
        self._interval_seconds = interval_seconds
        self._interval_strategy = interval_strategy
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def poll_once(self) -> bool:
        state = await self._fetch_state()
        return await self._broker.publish_if_changed(state)

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                self._broker.mark_spotify_error(exc)
                # Keep the local bridge alive if Spotify is briefly unavailable.
                pass
            await asyncio.sleep(self._next_interval_seconds())

    def _next_interval_seconds(self) -> float:
        if self._interval_strategy is None:
            return self._interval_seconds
        return max(self._interval_seconds, self._interval_strategy(self._interval_seconds))


class PeriodicPoller:
    def __init__(
        self,
        task: Callable[[], Awaitable[None]],
        interval_seconds: float,
        *,
        error_handler: Callable[[Exception], None] | None = None,
        interval_strategy: Callable[[float], float] | None = None,
    ) -> None:
        self._task_callback = task
        self._interval_seconds = interval_seconds
        self._error_handler = error_handler
        self._interval_strategy = interval_strategy
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def poll_once(self) -> None:
        await self._task_callback()

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                if self._error_handler is not None:
                    self._error_handler(exc)
            await asyncio.sleep(self._next_interval_seconds())

    def _next_interval_seconds(self) -> float:
        if self._interval_strategy is None:
            return self._interval_seconds
        return max(self._interval_seconds, self._interval_strategy(self._interval_seconds))
