from fastapi.testclient import TestClient

from app.art import ArtOptions, art_version, bytes_hash
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
    assert state["knob_art_url"] == "http://bridge.local:8090/v1/art/current.rgb565?size=180&swap=lvgl"
    assert state["knob_art_version"] == "ab67616d0000b273adfc1ac5836f96adac580271"


def test_knob_snapshot_shapes_render_contract_and_hashes(monkeypatch):
    previous = broker.current_state
    art_payload = b"\x00" * 64800

    async def fake_cached_rgb565_art(*args, **kwargs):
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
        raw={"context": {"type": "playlist", "uri": "spotify:playlist:playlist-id"}},
    )
    try:
        response = TestClient(app).get(
            "/v1/knob/snapshot?art_size=180&art_format=rgb565&swap=lvgl&art_variant=player-bg",
            headers={"host": "bridge.local:8090"},
        )
    finally:
        broker.current_state = previous

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["payload_hash"]) == 64
    assert len(payload["playback_hash"]) == 64
    assert payload["art_hash"] == bytes_hash(art_payload)
    assert payload["is_playing"] is True
    assert payload["track"]["artist_text"] == "Artist 1, Artist 2"
    assert payload["context"] == {
        "type": "playlist",
        "uri": "spotify:playlist:playlist-id",
        "name": None,
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
        "url": "http://bridge.local:8090/v1/knob/art/current.rgb565?size=180&swap=lvgl&variant=player-bg",
        "width": 180,
        "height": 180,
        "format": "rgb565",
        "byte_order": "lvgl-swap",
        "content_length": 64800,
    }
    assert payload["server"]["ok"] is True
