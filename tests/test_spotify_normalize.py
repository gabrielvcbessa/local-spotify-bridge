import httpx
import pytest

from app.config import Settings
from app.spotify import SpotifyClient, compact_track_preview, normalize_playback


def test_normalize_current_playback_payload():
    state = normalize_playback(
        {
            "is_playing": True,
            "progress_ms": 1234,
            "shuffle_state": False,
            "repeat_state": "off",
            "device": {
                "id": "dev1",
                "name": "Kitchen",
                "type": "Speaker",
                "is_active": True,
                "volume_percent": 42,
                "supports_volume": True,
            },
            "item": {
                "id": "track1",
                "uri": "spotify:track:track1",
                "type": "track",
                "name": "Tune",
                "duration_ms": 180000,
                "artists": [{"name": "Artist"}],
                "album": {
                    "name": "Album",
                    "images": [
                        {
                            "url": "https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271"
                        }
                    ],
                },
            },
        }
    )

    assert state.is_playing is True
    assert state.title == "Tune"
    assert state.artists == ["Artist"]
    assert state.album_art_url == "https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271"
    assert state.album_art_id == "ab67616d0000b273adfc1ac5836f96adac580271"
    assert state.knob_art_version == "ab67616d0000b273adfc1ac5836f96adac580271"
    assert state.device_name == "Kitchen"
    assert state.device_is_active is True
    assert state.device_volume_percent == 42
    assert state.volume_control_supported is True


def test_normalize_defaults_volume_control_to_false_when_unknown():
    state = normalize_playback(
        {
            "is_playing": True,
            "device": {"id": "dev1", "name": "Kitchen", "type": "Speaker"},
            "item": {"id": "track1", "name": "Tune"},
        }
    )

    assert state.volume_control_supported is False


def test_compact_track_preview_extracts_queue_track_metadata():
    preview = compact_track_preview(
        {
            "id": "next1",
            "uri": "spotify:track:next1",
            "type": "track",
            "name": "Next Tune",
            "duration_ms": 200000,
            "artists": [{"name": "Artist 1"}, {"name": "Artist 2"}],
            "album": {
                "name": "Next Album",
                "images": [{"url": "https://i.scdn.co/image/ab67616d0000b273next"}],
            },
        }
    )

    assert preview == {
        "id": "next1",
        "uri": "spotify:track:next1",
        "title": "Next Tune",
        "artists": ["Artist 1", "Artist 2"],
        "artist_text": "Artist 1, Artist 2",
        "album": "Next Album",
        "album_art_url": "https://i.scdn.co/image/ab67616d0000b273next",
        "album_art_id": "ab67616d0000b273next",
        "duration_ms": 200000,
    }


@pytest.mark.asyncio
async def test_current_playback_preloads_first_queue_track():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if str(request.url).endswith("/v1/me/player"):
            return httpx.Response(
                200,
                json={
                    "is_playing": True,
                    "device": {"id": "dev1"},
                    "item": {"id": "track1", "name": "Current", "type": "track"},
                },
            )
        if str(request.url).endswith("/v1/me/tracks/contains?ids=track1"):
            return httpx.Response(200, json=[True])
        if str(request.url).endswith("/v1/me/player/queue"):
            return httpx.Response(
                200,
                json={
                    "queue": [
                        {
                            "id": "next1",
                            "uri": "spotify:track:next1",
                            "type": "track",
                            "name": "Next Tune",
                            "artists": [{"name": "Artist"}],
                            "album": {
                                "name": "Next Album",
                                "images": [{"url": "https://i.scdn.co/image/ab67616d0000b273next"}],
                            },
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected URL {request.url}")

    client = SpotifyClient(
        Settings(
            SPOTIFY_CLIENT_ID="client",
            SPOTIFY_CLIENT_SECRET="secret",
            SPOTIFY_REFRESH_TOKEN="refresh",
            SPOTIFY_PRELOAD_NEXT_ENABLED=True,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    state = await client.current_playback()

    assert state is not None
    assert state.item_saved is True
    assert state.next_track is not None
    assert state.next_track["title"] == "Next Tune"
    await client.close()
