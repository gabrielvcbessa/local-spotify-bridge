import httpx
import pytest

from app import main
from app.config import Settings
from app.spotify import SpotifyClient
from app.store import RuntimeStore, TargetDevice


def test_authorize_url_contains_redirect_and_scopes():
    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client-id",
            SPOTIFY_CLIENT_SECRET="client-secret",
            SPOTIFY_REDIRECT_URI="http://localhost:8090/v1/auth/callback",
        )
    )

    url = client.authorize_url("state-1")

    assert "https://accounts.spotify.com/authorize?" in url
    assert "client_id=client-id" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8090%2Fv1%2Fauth%2Fcallback" in url
    assert "user-read-playback-state" in url
    assert "user-library-modify" in url
    assert "state=state-1" in url


@pytest.mark.anyio
async def test_exchange_code_returns_refresh_token():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://accounts.spotify.com/api/token"
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=abc123" in body
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "user-read-playback-state",
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = SpotifyClient(
            Settings(
                SPOTIFY_CLIENT_ID="client-id",
                SPOTIFY_CLIENT_SECRET="client-secret",
                SPOTIFY_REDIRECT_URI="http://localhost:8090/v1/auth/callback",
            ),
            http=http,
        )

        token = await client.exchange_code("abc123")

    assert token["refresh_token"] == "refresh"


def test_disconnect_runtime_credentials_clears_store_and_access_token(tmp_path):
    store = RuntimeStore(Settings(DATA_PATH=str(tmp_path / "state.json")))
    store.set_refresh_token("stored-refresh")
    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client-id",
            SPOTIFY_CLIENT_SECRET="client-secret",
            DATA_PATH=str(tmp_path / "state.json"),
        ),
        store=store,
    )
    client._access_token = "access"
    client._token_expires_at = 9999999999.0

    assert client.refresh_token_source == "runtime"

    env_configured = client.disconnect_runtime_credentials()

    assert env_configured is False
    assert store.get_refresh_token() is None
    assert client.refresh_token_source == "none"
    assert client._access_token is None
    assert client._token_expires_at == 0.0
    assert client.spotify_configured is False


def test_disconnect_runtime_credentials_reports_env_refresh_token(tmp_path):
    store = RuntimeStore(Settings(DATA_PATH=str(tmp_path / "state.json")))
    store.set_refresh_token("stored-refresh")
    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client-id",
            SPOTIFY_CLIENT_SECRET="client-secret",
            SPOTIFY_REFRESH_TOKEN="env-refresh",
            DATA_PATH=str(tmp_path / "state.json"),
        ),
        store=store,
    )

    assert client.refresh_token_source == "runtime"

    env_configured = client.disconnect_runtime_credentials()

    assert env_configured is True
    assert store.get_refresh_token() is None
    assert client.refresh_token_source == "environment"
    assert client.spotify_configured is True


@pytest.mark.anyio
async def test_auth_disconnect_publishes_status(monkeypatch):
    status_pulses = []

    class FakeSpotify:
        spotify_configured = False
        refresh_token_source = "none"

        def disconnect_runtime_credentials(self):
            return False

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None, command_metadata=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok, command_error))

    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    response = await main.auth_disconnect(client=FakeSpotify())

    assert response["persisted_refresh_token_cleared"] is True
    assert response["env_refresh_token_configured"] is False
    assert response["spotify_refresh_token_source"] == "none"
    assert response["spotify_configured"] is False
    assert status_pulses == [("disconnect_spotify", None, None, True, None)]


@pytest.mark.anyio
async def test_auth_callback_returns_token_free_metadata(monkeypatch):
    class FakePoller:
        started = False

        def start(self):
            self.started = True

    class FakeSpotify:
        refresh_token_source = "runtime"

        async def exchange_code(self, code):
            assert code == "abc123"
            return {
                "refresh_token": "secret-refresh-token",
                "expires_in": 3600,
                "scope": "user-read-playback-state",
                "token_type": "Bearer",
            }

    fake_poller = FakePoller()
    monkeypatch.setattr(main, "poller", fake_poller)

    response = await main.auth_callback(code="abc123", client=FakeSpotify())

    assert response["refresh_token_saved"] is True
    assert response["spotify_refresh_token_source"] == "runtime"
    assert "refresh_token" not in response
    assert "secret-refresh-token" not in str(response)
    assert fake_poller.started is True


@pytest.mark.anyio
async def test_playlist_name_falls_back_to_user_library_when_direct_lookup_fails():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://accounts.spotify.com/api/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.url == "https://api.spotify.com/v1/playlists/playlist-1?fields=id%2Cname%2Curi":
            return httpx.Response(404, json={"error": {"status": 404}})
        if request.url == "https://api.spotify.com/v1/me/playlists?limit=50&offset=0":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "other", "name": "Other Playlist"},
                        {"id": "playlist-1", "name": "Actual Playlist"},
                    ],
                    "next": None,
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = SpotifyClient(
            Settings(
                SPOTIFY_CLIENT_ID="client-id",
                SPOTIFY_CLIENT_SECRET="client-secret",
                SPOTIFY_REFRESH_TOKEN="refresh",
                SPOTIFY_REDIRECT_URI="http://localhost:8090/v1/auth/callback",
            ),
            http=http,
        )

        assert await client.playlist_name("playlist-1") == "Actual Playlist"


@pytest.mark.anyio
async def test_playlist_name_falls_back_to_oembed_when_web_api_cannot_resolve():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://accounts.spotify.com/api/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.url == "https://api.spotify.com/v1/playlists/playlist-1?fields=id%2Cname%2Curi":
            return httpx.Response(404, json={"error": {"status": 404}})
        if request.url == "https://api.spotify.com/v1/me/playlists?limit=50&offset=0":
            return httpx.Response(200, json={"items": [{"id": "other", "name": "Other"}], "next": None})
        if str(request.url) == "https://open.spotify.com/oembed?url=https%3A%2F%2Fopen.spotify.com%2Fplaylist%2Fplaylist-1":
            return httpx.Response(200, json={"title": "For My Hand (feat. Ed Sheeran) Radio"})
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = SpotifyClient(
            Settings(
                SPOTIFY_CLIENT_ID="client-id",
                SPOTIFY_CLIENT_SECRET="client-secret",
                SPOTIFY_REFRESH_TOKEN="refresh",
                SPOTIFY_REDIRECT_URI="http://localhost:8090/v1/auth/callback",
            ),
            http=http,
        )

        assert await client.playlist_name("playlist-1") == "For My Hand (feat. Ed Sheeran) Radio"


@pytest.mark.anyio
async def test_resolve_target_device_id_ignores_stale_stored_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://accounts.spotify.com/api/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.url == "https://api.spotify.com/v1/me/player/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {"id": "active-device", "name": "Gabriel's MacBook Air", "is_active": True},
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = SpotifyClient(
            Settings(
                SPOTIFY_CLIENT_ID="client-id",
                SPOTIFY_CLIENT_SECRET="client-secret",
                SPOTIFY_REFRESH_TOKEN="refresh",
                SPOTIFY_REDIRECT_URI="http://localhost:8090/v1/auth/callback",
            ),
            http=http,
        )

        resolved = await client.resolve_target_device_id(
            target=TargetDevice(device_id="stale-device")
        )

    assert resolved is None


@pytest.mark.anyio
async def test_resolve_target_device_id_matches_target_name_when_id_is_stale():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://accounts.spotify.com/api/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.url == "https://api.spotify.com/v1/me/player/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {"id": "active-device", "name": "Gabriel's MacBook Air", "is_active": True},
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = SpotifyClient(
            Settings(
                SPOTIFY_CLIENT_ID="client-id",
                SPOTIFY_CLIENT_SECRET="client-secret",
                SPOTIFY_REFRESH_TOKEN="refresh",
                SPOTIFY_REDIRECT_URI="http://localhost:8090/v1/auth/callback",
            ),
            http=http,
        )

        resolved = await client.resolve_target_device_id(
            target=TargetDevice(device_id="stale-device", device_name="Gabriel's MacBook Air")
        )

    assert resolved == "active-device"
