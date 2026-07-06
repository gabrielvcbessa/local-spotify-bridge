import asyncio
import json
from collections.abc import Awaitable, Callable

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
        self._version = 0
        self.current_state: PlaybackSnapshot | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._settings.mqtt_enabled:
            return
        if mqtt is None:
            raise RuntimeError("paho-mqtt is required when MQTT_ENABLED=true.")

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self._settings.mqtt_username:
            client.username_pw_set(self._settings.mqtt_username, self._settings.mqtt_password or None)
        client.connect(self._settings.mqtt_host, self._settings.mqtt_port, 60)
        client.loop_start()
        self._mqtt_client = client

    async def stop(self) -> None:
        if self._mqtt_client is not None:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None

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
            self._mqtt_client.publish(topic, text, qos=1, retain=True)


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
            except Exception:
                # Keep the local bridge alive if Spotify is briefly unavailable.
                pass
            await asyncio.sleep(self._interval_seconds)

