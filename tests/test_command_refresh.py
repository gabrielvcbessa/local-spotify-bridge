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

    async def next_track(self, device_id: str | None = None) -> None:
        self.calls.append(("next", device_id))

    async def previous_track(self, device_id: str | None = None) -> None:
        self.calls.append(("previous", device_id))


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
