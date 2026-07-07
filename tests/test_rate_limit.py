import httpx
import pytest

from app.broker import ConnectionBroker, PeriodicPoller, StatePoller
from app.config import Settings
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


def test_periodic_poller_uses_adaptive_backoff_without_going_faster_than_base():
    async def task():
        return None

    poller = PeriodicPoller(task, 30, interval_strategy=lambda _: 10)

    assert poller._next_interval_seconds() == 30

    poller = PeriodicPoller(task, 30, interval_strategy=lambda _: 45)

    assert poller._next_interval_seconds() == 45


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
