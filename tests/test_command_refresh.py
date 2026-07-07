import pytest

import app.main as main
from app.models import PlaybackSnapshot


class FakeSpotifyClient:
    def __init__(self) -> None:
        self.calls = 0

    async def current_playback(self) -> PlaybackSnapshot:
        self.calls += 1
        return PlaybackSnapshot(title=f"Song {self.calls}")


class FakeCommandSpotifyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def play(self, body=None, device_id: str | None = None) -> None:
        self.calls.append(("play", device_id))

    async def pause(self, device_id: str | None = None) -> None:
        self.calls.append(("pause", device_id))

    async def next_track(self, device_id: str | None = None) -> None:
        self.calls.append(("next", device_id))

    async def previous_track(self, device_id: str | None = None) -> None:
        self.calls.append(("previous", device_id))


class FakeDevicesClient:
    def __init__(self) -> None:
        self.devices_calls = 0
        self.playlists_calls = 0
        self.saved_tracks_calls = 0

    async def devices(self):
        self.devices_calls += 1
        return {
            "devices": [
                {
                    "id": "device-1",
                    "name": "Speaker",
                    "type": "Speaker",
                    "is_active": True,
                    "supports_volume": True,
                    "volume_percent": 42,
                }
            ]
        }

    async def playlists(self, *, limit: int, offset: int):
        self.playlists_calls += 1
        return {"total": 3, "items": []}

    async def saved_tracks(self, *, limit: int, offset: int):
        self.saved_tracks_calls += 1
        return {"total": 5, "items": []}


@pytest.mark.asyncio
async def test_refresh_and_publish_schedules_follow_up_refreshes(monkeypatch):
    client = FakeSpotifyClient()
    published: list[str | None] = []
    scheduled = []

    async def fake_publish_if_changed(state):
        published.append(state.title if state else None)
        return True

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(main.broker, "publish_if_changed", fake_publish_if_changed)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    await main.refresh_and_publish(client, follow_up_delays=(0.5, 1.5))

    assert published == ["Song 1"]
    assert len(scheduled) == 2


@pytest.mark.asyncio
async def test_refresh_devices_and_publish_updates_cache_and_retained_topic(monkeypatch):
    client = FakeDevicesClient()
    published = []
    previous_cached = main.cached_devices

    async def fake_publish_mqtt_retained(topic, payload):
        published.append((topic, payload))

    monkeypatch.setattr(main.broker, "publish_mqtt_retained", fake_publish_mqtt_retained)
    main.cached_devices = None
    try:
        payload = await main.refresh_devices_and_publish(client)
        cached = main.cached_devices
    finally:
        main.cached_devices = previous_cached

    assert client.devices_calls == 1
    assert cached is not None
    assert cached[0]["id"] == "device-1"
    assert payload["items"][0]["id"] == "device-1"
    assert published == [("devices", payload)]


@pytest.mark.asyncio
async def test_library_root_uses_cached_devices_without_fetching_devices():
    client = FakeDevicesClient()
    previous_cached = main.cached_devices
    main.cached_devices = [{"id": "cached-device"}]
    try:
        payload = await main.build_library_root_payload(client)
    finally:
        main.cached_devices = previous_cached

    assert client.devices_calls == 0
    assert payload["pages"][2]["total"] == 1


@pytest.mark.asyncio
async def test_mqtt_next_previous_do_not_use_implicit_target_device(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status():
        return None

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.handle_mqtt_command({"type": "next"})
    await main.handle_mqtt_command({"type": "previous"})
    await main.handle_mqtt_command({"type": "next", "device_id": "speaker-1"})

    assert client.calls == [
        ("next", None),
        ("previous", None),
        ("next", "speaker-1"),
    ]
    assert len(refreshes) == 3


@pytest.mark.asyncio
async def test_mqtt_play_pause_does_not_use_implicit_target_device(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status():
        return None

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    main.broker.current_state = PlaybackSnapshot(is_playing=True)
    try:
        await main.handle_mqtt_command({"type": "play_pause"})
        main.broker.current_state = PlaybackSnapshot(is_playing=False)
        await main.handle_mqtt_command({"type": "play_pause"})
        await main.handle_mqtt_command({"type": "pause", "device_id": "speaker-1"})
    finally:
        main.broker.current_state = None

    assert client.calls == [
        ("pause", None),
        ("play", None),
        ("pause", "speaker-1"),
    ]
    assert len(refreshes) == 3
