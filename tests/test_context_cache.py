import pytest

from app.context_cache import PlaylistNameCache, context_id_from_uri, playback_context_parts
from app.models import PlaybackSnapshot


def test_context_id_from_uri_parses_playlist_safely():
    assert context_id_from_uri("spotify:playlist:abc123") == "abc123"
    assert context_id_from_uri("https://example.test/playlist/abc123") is None
    assert context_id_from_uri("spotify:playlist") is None


def test_playback_context_parts_extracts_type_uri_and_id():
    state = PlaybackSnapshot(raw={"context": {"uri": "spotify:playlist:abc123"}})

    assert playback_context_parts(state) == {
        "type": "playlist",
        "uri": "spotify:playlist:abc123",
        "id": "abc123",
        "name": None,
    }


def test_playback_context_parts_extracts_direct_context_name():
    state = PlaybackSnapshot(
        raw={
            "context": {
                "uri": "spotify:playlist:abc123",
                "name": "Daily Lift",
            }
        }
    )

    assert playback_context_parts(state)["name"] == "Daily Lift"


@pytest.mark.anyio
async def test_playlist_name_cache_caches_success():
    cache = PlaylistNameCache()
    calls = 0

    async def resolver(playlist_id: str) -> str:
        nonlocal calls
        calls += 1
        return f"Playlist {playlist_id}"

    assert await cache.resolve_once("abc123", resolver) == "Playlist abc123"
    assert await cache.resolve_once("abc123", resolver) == "Playlist abc123"
    assert cache.get("abc123") == "Playlist abc123"
    assert calls == 1


@pytest.mark.anyio
async def test_playlist_name_cache_failure_is_temporarily_cached():
    cache = PlaylistNameCache()
    calls = 0

    async def resolver(_: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await cache.resolve_once("abc123", resolver)

    assert await cache.resolve_once("abc123", resolver) is None
    assert calls == 1
