from fastapi.testclient import TestClient

from app.art import ArtOptions, art_version, bytes_hash
from app.knob import knob_snapshot
import app.main as main
from app.main import app, broker
from app.models import PlaybackSnapshot


def test_state_adds_stable_knob_art_url_and_version():
    previous = broker.current_state
    broker.current_state = PlaybackSnapshot(
        album_art_url="https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271",
        album_art_id="ab67616d0000b273adfc1ac5836f96adac580271",
        knob_art_version="ab67616d0000b273adfc1ac5836f96adac580271",
    )
    try:
        response = TestClient(app).get("/v1/state", headers={"host": "bridge.local:8090"})
    finally:
        broker.current_state = previous

    assert response.status_code == 200
    state = response.json()["state"]
    assert state["knob_art_url"] == "http://bridge.local:8090/v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg"
    assert state["knob_art_version"] == "ab67616d0000b273adfc1ac5836f96adac580271"


def test_knob_snapshot_defaults_to_stopwatch_art_size(monkeypatch):
    previous = broker.current_state
    art_payload = b"\x00" * 259200

    async def fake_cached_rgb565_art(*args, **kwargs):
        return art_payload

    monkeypatch.setattr(main, "cached_rgb565_art", fake_cached_rgb565_art)
    broker.current_state = PlaybackSnapshot(
        album_art_url="https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271",
        album_art_id="ab67616d0000b273adfc1ac5836f96adac580271",
    )
    try:
        response = TestClient(app).get("/v1/knob/snapshot", headers={"host": "bridge.local:8090"})
    finally:
        broker.current_state = previous

    assert response.status_code == 200
    art = response.json()["art"]
    assert art["url"] == "http://bridge.local:8090/v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg"
    assert art["width"] == 360
    assert art["height"] == 360
    assert art["content_length"] == 259200
    assert response.json()["art_hash"] == bytes_hash(art_payload)


def test_mqtt_knob_config_defaults_to_stopwatch_art_size():
    config = main.mqtt_knob_config()

    assert main.mqtt_art_options().size == 360
    assert config["topics"]["control_state"] == "rotary/knob/control_state"
    assert config["retain"]["control_state"] is True
    assert config["http"]["art_url"] == (
        "http://localhost:8090/v1/knob/art/current.rgb565"
        "?size=360&format=rotary-lvgl&variant=player-bg"
    )
    assert config["art"]["topics"] == {
        "current": "rotary/knob/art/current/rgb565",
        "next": "rotary/knob/art/next/rgb565",
        "previous": "rotary/knob/art/previous/rgb565",
    }


def test_mqtt_art_annotation_adds_topic_and_local_cache_path():
    state = PlaybackSnapshot(
        album_art_url="https://i.scdn.co/image/current-art",
        album_art_id="current-art",
        next_track={
            "id": "next-track",
            "title": "Next",
            "album_art_id": "next-art",
            "album_art_url": "https://i.scdn.co/image/next-art",
        },
    )
    snapshot = knob_snapshot(
        version=1,
        state=state,
        base_url="http://bridge.local:8090",
        spotify_configured=True,
        art_options=ArtOptions(size=240, swap="lvgl", variant="player-bg"),
    )

    main.annotate_mqtt_art(snapshot, state, ArtOptions(size=240, swap="lvgl", variant="player-bg"))

    assert snapshot["art"]["mqtt_topic"] == "rotary/knob/art/current/rgb565"
    assert snapshot["art"]["local_cache_path"].endswith("current-art-size240-themedark-swaplvgl-variantplayer-bg-blur0-dark0.52-sat0.9-contrast1.08-circle0.rgb565")
    assert snapshot["next_track"]["art"]["mqtt_topic"] == "rotary/knob/art/next/rgb565"
    assert snapshot["next_track"]["art"]["local_cache_path"].endswith("next-art-size240-themedark-swaplvgl-variantplayer-bg-blur0-dark0.52-sat0.9-contrast1.08-circle0.rgb565")


def test_knob_snapshot_shapes_render_contract_and_hashes(monkeypatch):
    previous = broker.current_state
    art_payload = b"\x00" * 64800
    cached_art_calls = []

    async def fake_cached_rgb565_art(_client, image_id, _url, options):
        cached_art_calls.append((image_id, options.size))
        return art_payload

    monkeypatch.setattr(main, "cached_rgb565_art", fake_cached_rgb565_art)
    broker.current_state = PlaybackSnapshot(
        is_playing=True,
        progress_ms=12345,
        duration_ms=180000,
        item_id="spotify-track-id",
        item_uri="spotify:track:spotify-track-id",
        title="Song name",
        artists=["Artist 1", "Artist 2"],
        album="Album name",
        album_art_url="https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271",
        album_art_id="ab67616d0000b273adfc1ac5836f96adac580271",
        device_id="spotify-device-id",
        device_name="Living Room Speaker",
        device_type="Smartphone",
        device_is_active=True,
        device_volume_percent=42,
        volume_control_supported=True,
        shuffle_state=False,
        repeat_state="off",
        next_track={
            "id": "next-track-id",
            "uri": "spotify:track:next-track-id",
            "title": "Next song",
            "artists": ["Artist 3"],
            "artist_text": "Artist 3",
            "album": "Next album",
            "album_art_url": "https://i.scdn.co/image/ab67616d0000b273next",
            "album_art_id": "ab67616d0000b273next",
            "duration_ms": 181000,
        },
        previous_track={
            "id": "previous-track-id",
            "uri": "spotify:track:previous-track-id",
            "title": "Previous song",
            "artists": ["Artist 0"],
            "artist_text": "Artist 0",
            "album": "Previous album",
            "album_art_url": "https://i.scdn.co/image/ab67616d0000b273previous",
            "album_art_id": "ab67616d0000b273previous",
            "duration_ms": 179000,
            "context_uri": "spotify:playlist:playlist-id",
            "album_uri": "spotify:album:previous-album-id",
        },
        raw={"context": {"type": "playlist", "uri": "spotify:playlist:playlist-id"}},
    )
    try:
        response = TestClient(app).get(
            "/v1/knob/snapshot?art_size=180&art_format=rotary-lvgl&art_variant=player-bg",
            headers={"host": "bridge.local:8090"},
        )
    finally:
        broker.current_state = previous

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["payload_hash"]) == 64
    assert len(payload["playback_hash"]) == 64
    assert payload["art_hash"] == bytes_hash(art_payload)
    assert cached_art_calls == [
        ("ab67616d0000b273adfc1ac5836f96adac580271", 180),
        ("ab67616d0000b273next", 180),
        ("ab67616d0000b273previous", 180),
    ]
    assert payload["is_playing"] is True
    assert payload["track"]["artist_text"] == "Artist 1, Artist 2"
    assert payload["next_track"]["title"] == "Next song"
    assert payload["next_track"]["art"] == {
        "id": "ab67616d0000b273next",
        "version": art_version(
            "ab67616d0000b273next",
            ArtOptions(size=180, swap="lvgl", variant="player-bg"),
        ),
        "url": "http://bridge.local:8090/v1/art/ab67616d0000b273next.rgb565?size=180&swap=lvgl&variant=player-bg",
        "width": 180,
        "height": 180,
        "format": "rgb565",
        "byte_order": "rotary-lvgl",
        "content_length": 64800,
    }
    assert payload["previous_track"]["title"] == "Previous song"
    assert payload["previous_track"]["art"] == {
        "id": "ab67616d0000b273previous",
        "version": art_version(
            "ab67616d0000b273previous",
            ArtOptions(size=180, swap="lvgl", variant="player-bg"),
        ),
        "url": "http://bridge.local:8090/v1/art/ab67616d0000b273previous.rgb565?size=180&swap=lvgl&variant=player-bg",
        "width": 180,
        "height": 180,
        "format": "rgb565",
        "byte_order": "rotary-lvgl",
        "content_length": 64800,
    }
    assert payload["context"] == {
        "type": "playlist",
        "uri": "spotify:playlist:playlist-id",
        "id": "playlist-id",
        "name": None,
        "display_name": "Album name",
        "fallback_name": "Album name",
    }
    assert payload["device"]["can_control_playback"] is True
    assert payload["device"]["volume_control_supported"] is True
    assert payload["modes"] == {"shuffle": False, "repeat": "off"}
    assert payload["art"] == {
        "id": "ab67616d0000b273adfc1ac5836f96adac580271",
        "version": art_version(
            "ab67616d0000b273adfc1ac5836f96adac580271",
            ArtOptions(size=180, swap="lvgl", variant="player-bg"),
        ),
        "hash": bytes_hash(art_payload),
        "variant": "player-bg",
        "url": "http://bridge.local:8090/v1/knob/art/current.rgb565?size=180&format=rotary-lvgl&variant=player-bg",
        "width": 180,
        "height": 180,
        "format": "rgb565",
        "byte_order": "rotary-lvgl",
        "content_length": 64800,
    }
    assert payload["server"]["ok"] is True


def test_knob_test_pattern_endpoint_sets_rotary_lvgl_contract_headers():
    response = TestClient(app).get("/v1/knob/art/test-pattern.rgb565?size=180&format=rotary-lvgl")

    assert response.status_code == 200
    assert response.headers["X-Image-Format"] == "rgb565"
    assert response.headers["X-Image-Byte-Order"] == "rotary-lvgl"
    assert response.headers["X-Image-Target"] == "rotary-os-lvgl-image-source"
    assert response.headers["Content-Length"] == str(180 * 180 * 2)
    assert response.content[0:2] == bytes.fromhex("00f8")
    assert response.content[36 * 2:36 * 2 + 2] == bytes.fromhex("e007")


def test_knob_snapshot_uses_resolved_playlist_display_name():
    state = PlaybackSnapshot(
        album="Album fallback",
        raw={"context": {"uri": "spotify:playlist:playlist-id"}},
    )

    unresolved = knob_snapshot(
        version=1,
        state=state,
        base_url="http://bridge.local:8090",
        spotify_configured=True,
        art_options=ArtOptions(),
    )
    resolved = knob_snapshot(
        version=1,
        state=state,
        base_url="http://bridge.local:8090",
        spotify_configured=True,
        art_options=ArtOptions(),
        context_name="Playlist name",
    )

    assert unresolved["context"]["display_name"] == "Album fallback"
    assert resolved["context"]["name"] == "Playlist name"
    assert resolved["context"]["display_name"] == "Playlist name"
    assert unresolved["playback_hash"] != resolved["playback_hash"]
