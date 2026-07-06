import pytest

import app.main as main
from app.models import PlaybackSnapshot


class FakeSpotifyClient:
    def __init__(self) -> None:
        self.calls = 0

    async def current_playback(self) -> PlaybackSnapshot:
        self.calls += 1
        return PlaybackSnapshot(title=f"Song {self.calls}")


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
