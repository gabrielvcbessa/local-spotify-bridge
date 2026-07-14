from app.art import ArtOptions
from app.models import PlaybackSnapshot
from app.mqtt_contract import (
    MQTT_KNOB_BACKEND_CAPABILITIES,
    MQTT_KNOB_FEATURES,
    MQTT_KNOB_COMMANDS,
    MQTT_KNOB_SCHEMA_VERSION,
    mqtt_control_state_payload,
    mqtt_knob_config_payload,
)


def test_mqtt_knob_config_payload_advertises_protocol_and_topics():
    topics = {
        "state": "rotary/knob/state",
        "control_state": "rotary/knob/control_state",
        "config": "rotary/knob/config",
        "command": "rotary/knob/command",
        "command_result": "rotary/knob/command_result",
        "availability": "rotary/knob/availability",
        "library_root": "rotary/knob/library/root",
        "library_page": "rotary/knob/library/page",
        "library_playlists": "rotary/knob/library/playlists",
        "devices": "rotary/knob/devices",
        "status": "rotary/knob/status",
        "request": "rotary/knob/request",
        "request_result": "rotary/knob/request_result",
        "art_current": "rotary/knob/art/current/rgb565",
        "art_next": "rotary/knob/art/next/rgb565",
        "art_previous": "rotary/knob/art/previous/rgb565",
    }

    payload = mqtt_knob_config_payload(
        device_id="knob",
        qos=1,
        topics=topics,
        base_url="http://bridge.local:8090",
        art_options=ArtOptions(size=360, swap="lvgl", variant="player-bg"),
    )

    assert payload["schema_version"] == MQTT_KNOB_SCHEMA_VERSION
    assert payload["protocol"]["name"] == "rotary-mqtt-knob"
    assert payload["protocol"]["features"] == MQTT_KNOB_FEATURES
    assert payload["topics"]["control_state"] == "rotary/knob/control_state"
    assert payload["retain"]["control_state"] is True
    assert payload["art"]["topics"]["current"] == "rotary/knob/art/current/rgb565"
    assert payload["capabilities"] == MQTT_KNOB_BACKEND_CAPABILITIES
    assert payload["capabilities"]["transport"] == "spotify_web_api"
    assert payload["capabilities"]["devices"]["readiness"] is True
    assert "status_command_ok" in payload["protocol"]["features"]
    assert "status_command_error" in payload["protocol"]["features"]
    assert "target_ready" in payload["capabilities"]["runtime_states"]
    assert "save_current_track" in MQTT_KNOB_COMMANDS
    assert "unsave_current_track" in MQTT_KNOB_COMMANDS


def test_mqtt_control_state_payload_is_small_fast_state():
    payload = mqtt_control_state_payload(
        9,
        PlaybackSnapshot(
            is_playing=True,
            item_id="track-1",
            item_uri="spotify:track:track-1",
            item_saved=True,
            title="Song",
            artists=["Artist"],
            progress_ms=123,
            duration_ms=456,
            device_id="device-1",
            device_name="Speaker",
            device_volume_percent=42,
            volume_control_supported=True,
            shuffle_state=False,
            repeat_state="off",
        ),
    )

    assert payload["version"] == 9
    assert payload["playing"] is True
    assert payload["track_id"] == "track-1"
    assert payload["track_saved"] is True
    assert payload["artist_text"] == "Artist"
    assert payload["device"]["id"] == "device-1"
    assert payload["device"]["volume_control_supported"] is True
    assert "art" not in payload
