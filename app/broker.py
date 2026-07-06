import asyncio
import json
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
        self._version = 0
        self.current_state: PlaybackSnapshot | None = None
        self.last_poll_at: str | None = None
        self.last_spotify_error: str | None = None
        self.last_mqtt_availability: dict[str, Any] | None = None
        self.last_mqtt_availability_at: str | None = None
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
        changed = states_are_meaningfully_different(
            self.current_state,
            new_state,
            progress_drift_ms=self._settings.state_change_progress_drift_ms,
        )
        if not changed:
            return False

        self.current_state = new_state
        self._version += 1
        await self.publish("playback.changed", new_state)
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
            self._mqtt_client.publish(topic, text, qos=self._settings.mqtt_qos, retain=True)
            if self._mqtt_snapshot_factory is not None:
                snapshot = await self._mqtt_snapshot_factory(self._version, state)
                self._mqtt_client.publish(
                    self.mqtt_topic("state"),
                    json.dumps(snapshot),
                    qos=self._settings.mqtt_qos,
                    retain=True,
                )

    async def publish_mqtt_config(self) -> None:
        if self._mqtt_client is None or self._mqtt_config_factory is None:
            return
        self._mqtt_client.publish(
            self.mqtt_topic("config"),
            json.dumps(self._mqtt_config_factory()),
            qos=self._settings.mqtt_qos,
            retain=True,
        )

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
        }

    def _on_mqtt_connect(self, client, _, __, reason_code, ___) -> None:
        if reason_code != 0:
            self.last_spotify_error = f"MQTT connect failed: {reason_code}"
            return
        client.subscribe(self.mqtt_topic("command"), qos=self._settings.mqtt_qos)
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
        if topic != self.mqtt_topic("command") or self._mqtt_command_handler is None:
            return

        try:
            command = json.loads(payload)
            if not isinstance(command, dict):
                raise ValueError("MQTT command payload must be a JSON object.")
            result = await self._mqtt_command_handler(command)
            response = {"ok": True, "command": command.get("type"), "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}

        if self._mqtt_client is not None:
            self._mqtt_client.publish(
                self.mqtt_topic("command_result"),
                json.dumps(response),
                qos=self._settings.mqtt_qos,
                retain=False,
            )


class StatePoller:
    def __init__(
        self,
        fetch_state: Callable[[], Awaitable[PlaybackSnapshot | None]],
        broker: ConnectionBroker,
        interval_seconds: float,
    ) -> None:
        self._fetch_state = fetch_state
        self._broker = broker
        self._interval_seconds = interval_seconds
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
            await asyncio.sleep(self._interval_seconds)
