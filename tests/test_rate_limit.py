import httpx
import pytest

from app.broker import ConnectionBroker, PeriodicPoller, StatePoller
from app.config import Settings
from app.models import PlaybackSnapshot
from app.rate_limit import SpotifyRateLimiter
from app.spotify import SpotifyClient


def test_poll_interval_stays_at_base_when_under_threshold():
    limiter = SpotifyRateLimiter(
        window_seconds=30,
        soft_requests_per_window=10,
        soft_ratio=0.8,
    )
    for _ in range(7):
        limiter.record_request(now=100)

    assert limiter.poll_interval(3, now=100) == 3


def test_poll_interval_increases_near_soft_threshold():
    limiter = SpotifyRateLimiter(
        window_seconds=30,
        soft_requests_per_window=10,
        soft_ratio=0.8,
        backoff_multiplier=1.5,
    )
    for _ in range(8):
        limiter.record_request(now=100)

    assert limiter.poll_interval(3, now=100) > 3


def test_retry_after_blocks_future_requests_and_polling():
    limiter = SpotifyRateLimiter(retry_after_padding_seconds=0.5)

    limiter.record_response(429, retry_after="4", now=100)

    assert limiter.wait_seconds(now=101) == 3.5
    assert limiter.poll_interval(3, now=101) == 3.5


def test_rolling_window_prunes_old_requests():
    limiter = SpotifyRateLimiter(window_seconds=30, soft_requests_per_window=3, soft_ratio=0.5)

    limiter.record_request(now=100)
    limiter.record_request(now=103)
    limiter.record_request(now=132)

    status = limiter.status(3, now=132)
    assert status["requests_in_window"] == 2


def test_state_poller_never_goes_faster_than_base_interval():
    async def fetch_state():
        return None

    broker = ConnectionBroker(Settings())
    poller = StatePoller(fetch_state, broker, 3, interval_strategy=lambda _: 1)

    assert poller._next_interval_seconds() == 3


def test_state_poller_uses_shared_idle_interval_when_no_consumers_are_active():
    async def fetch_state():
        return None

    broker = ConnectionBroker(Settings())
    poller = StatePoller(
        fetch_state,
        broker,
        3,
        idle_interval_seconds=300,
        active_strategy=lambda: False,
    )

    assert poller._next_interval_seconds() == 300

    poller = StatePoller(
        fetch_state,
        broker,
        3,
        idle_interval_seconds=300,
        active_strategy=lambda: True,
    )

    assert poller._next_interval_seconds() == 3


@pytest.mark.asyncio
async def test_state_poller_wakes_near_track_end_when_consumers_are_active():
    async def fetch_state():
        return None

    broker = ConnectionBroker(Settings())
    await broker.publish_if_changed(
        PlaybackSnapshot(is_playing=True, progress_ms=29_500, duration_ms=30_000, title="Almost done")
    )
    poller = StatePoller(
        fetch_state,
        broker,
        5,
        active_strategy=lambda: True,
        track_end_refresh_padding_seconds=1,
    )

    assert poller._next_interval_seconds() == 1.5


@pytest.mark.asyncio
async def test_state_poller_keeps_idle_lower_bound_near_track_end_without_consumers():
    async def fetch_state():
        return None

    broker = ConnectionBroker(Settings())
    await broker.publish_if_changed(
        PlaybackSnapshot(is_playing=True, progress_ms=29_500, duration_ms=30_000, title="Almost done")
    )
    poller = StatePoller(
        fetch_state,
        broker,
        5,
        idle_interval_seconds=300,
        active_strategy=lambda: False,
        track_end_refresh_padding_seconds=1,
    )

    assert poller._next_interval_seconds() == 300


def test_periodic_poller_uses_adaptive_backoff_without_going_faster_than_base():
    async def task():
        return None

    poller = PeriodicPoller(task, 30, interval_strategy=lambda _: 10)

    assert poller._next_interval_seconds() == 30

    poller = PeriodicPoller(task, 30, interval_strategy=lambda _: 45)

    assert poller._next_interval_seconds() == 45


def test_periodic_poller_uses_shared_idle_interval_when_no_consumers_are_active():
    async def task():
        return None

    poller = PeriodicPoller(
        task,
        30,
        idle_interval_seconds=300,
        active_strategy=lambda: False,
    )

    assert poller._next_interval_seconds() == 300

    poller = PeriodicPoller(
        task,
        30,
        idle_interval_seconds=300,
        active_strategy=lambda: True,
    )

    assert poller._next_interval_seconds() == 30


def test_periodic_poller_idle_interval_does_not_go_below_active_base():
    async def task():
        return None

    poller = PeriodicPoller(
        task,
        7200,
        idle_interval_seconds=300,
        active_strategy=lambda: False,
    )

    assert poller._next_interval_seconds() == 7200


@pytest.mark.asyncio
async def test_spotify_client_records_retry_after_from_429():
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate limited"})

    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client",
            SPOTIFY_CLIENT_SECRET="secret",
            SPOTIFY_REFRESH_TOKEN="refresh",
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.request("GET", "/me/player")

    status = client.rate_limit_status(3)
    assert status["last_retry_after_seconds"] == 5
    assert status["retry_after_remaining_seconds"] > 4
    assert any("api.spotify.com" in request for request in requests)

    await client.close()


@pytest.mark.asyncio
async def test_spotify_playlist_429_does_not_block_playback_refresh(monkeypatch):
    requests: list[str] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(429, headers={"Retry-After": "600"}, json={"error": "playlist limited"})
        if request.url.path == "/v1/me/player":
            return httpx.Response(
                200,
                json={
                    "is_playing": True,
                    "progress_ms": 1000,
                    "item": {
                        "id": "track-1",
                        "uri": "spotify:track:track-1",
                        "type": "track",
                        "name": "Song",
                        "duration_ms": 180000,
                        "artists": [],
                        "album": {"name": "Album", "images": []},
                    },
                    "device": {"id": "device-1", "name": "Speaker", "supports_volume": True},
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr("app.spotify.asyncio.sleep", fake_sleep)
    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client",
            SPOTIFY_CLIENT_SECRET="secret",
            SPOTIFY_REFRESH_TOKEN="refresh",
            SPOTIFY_PRELOAD_NEXT_ENABLED=False,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.playlists(limit=1, offset=0)

    state = await client.current_playback()

    assert state is not None
    assert state.title == "Song"
    assert sleeps == []
    assert client.rate_limit_status(5, group="playlists")["retry_after_remaining_seconds"] > 500
    assert client.rate_limit_status(5, group="playback")["retry_after_remaining_seconds"] == 0
    assert any("/v1/me/player" in request for request in requests)

    await client.close()


@pytest.mark.asyncio
async def test_spotify_command_accepts_successful_text_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path == "/v1/me/player/next":
            return httpx.Response(200, text="opaque-command-id")
        return httpx.Response(404)

    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client",
            SPOTIFY_CLIENT_SECRET="secret",
            SPOTIFY_REFRESH_TOKEN="refresh",
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await client.next_track()

    await client.close()
