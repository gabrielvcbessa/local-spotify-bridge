import httpx
import pytest

from app.config import Settings
from app.spotify import SpotifyClient


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
