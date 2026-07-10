from typing import Any

from .art import ArtOptions
from .knob_mqtt import envelope
from .models import PlaybackSnapshot


MQTT_KNOB_PROTOCOL_NAME = "rotary-mqtt-knob"
MQTT_KNOB_SCHEMA_VERSION = 2
MQTT_KNOB_MIN_CLIENT_SCHEMA_VERSION = 2
MQTT_KNOB_MAX_CLIENT_SCHEMA_VERSION = 2

MQTT_KNOB_FEATURES = [
    "control_state",
    "library_browse",
    "devices",
    "command_request_id",
    "idempotent_command_result",
    "command_latency",
    "retained_rgb565_art",
]

MQTT_KNOB_COMMANDS = [
    "play_pause",
    "play",
    "pause",
    "next",
    "previous",
    "volume_set",
    "seek",
    "select_source",
    "transfer",
    "shuffle_set",
    "repeat_set",
    "play_library_item",
]

MQTT_KNOB_REQUESTS = ["library_root", "library_page", "library_playlists", "devices", "refresh"]


def mqtt_protocol_payload() -> dict[str, Any]:
    return {
        "name": MQTT_KNOB_PROTOCOL_NAME,
        "schema_version": MQTT_KNOB_SCHEMA_VERSION,
        "min_client_schema_version": MQTT_KNOB_MIN_CLIENT_SCHEMA_VERSION,
        "max_client_schema_version": MQTT_KNOB_MAX_CLIENT_SCHEMA_VERSION,
        "features": MQTT_KNOB_FEATURES,
    }


def mqtt_knob_config_payload(
    *,
    device_id: str,
    qos: int,
    topics: dict[str, str],
    base_url: str,
    art_options: ArtOptions,
) -> dict[str, Any]:
    return {
        "schema_version": MQTT_KNOB_SCHEMA_VERSION,
        "protocol": mqtt_protocol_payload(),
        "device_id": device_id,
        "qos": qos,
        "retain": {
            "state": True,
            "control_state": True,
            "config": True,
            "library_root": True,
            "library_page": True,
            "library_playlists": True,
            "devices": True,
            "status": True,
            "command_result": False,
            "request_result": False,
        },
        "topics": topics,
        "http": {
            "base_url": base_url,
            "snapshot_url": f"{base_url}/v1/knob/snapshot",
            "art_url": (
                f"{base_url}/v1/knob/art/current.rgb565"
                f"?size={art_options.size}&format=rotary-lvgl&variant={art_options.variant}"
            ),
        },
        "art": {
            "size": art_options.size,
            "format": "rgb565",
            "swap": art_options.swap,
            "variant": art_options.variant,
            "byte_order": art_options.byte_order,
            "topics": {
                "current": topics["art_current"],
                "next": topics["art_next"],
                "previous": topics["art_previous"],
            },
        },
        "commands": MQTT_KNOB_COMMANDS,
        "requests": MQTT_KNOB_REQUESTS,
        "limits": {
            "knob_visible_rows": 3,
            "library_page_limit": 3,
            "max_title_chars": 64,
            "max_subtitle_chars": 64,
        },
    }


def mqtt_control_state_payload(version: int, state: PlaybackSnapshot | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "playing": bool(state.is_playing) if state else False,
        "track_id": state.item_id if state else None,
        "track_uri": state.item_uri if state else None,
        "title": state.title if state else None,
        "artist_text": ", ".join(state.artists) if state and state.artists else None,
        "progress_ms": state.progress_ms if state else None,
        "duration_ms": state.duration_ms if state else None,
        "device": {
            "id": state.device_id if state else None,
            "name": state.device_name if state else None,
            "type": state.device_type if state else None,
            "is_active": state.device_is_active if state else None,
            "volume_percent": state.device_volume_percent if state else None,
            "volume_control_supported": state.volume_control_supported if state else False,
        },
        "shuffle": state.shuffle_state if state else None,
        "repeat": state.repeat_state if state else None,
    }
    return envelope(version=version, payload=payload)
