from typing import Any

from .art import ArtOptions
from .context_cache import playback_context_parts
from .knob_mqtt import envelope
from .mqtt_commands import MQTT_READY_TARGET_GUARDED_COMMANDS
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
    "command_state_refresh_result",
    "command_device_refresh_result",
    "command_result_metadata",
    "command_playback_expectation",
    "queue_status_metadata",
    "status_command_metadata",
    "status_command_ok",
    "status_command_error",
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
    "save_current_track",
    "unsave_current_track",
    "play_library_item",
]

MQTT_KNOB_REQUESTS = ["library_root", "library_page", "library_playlists", "devices", "refresh"]

MQTT_KNOB_BACKEND_CAPABILITIES: dict[str, Any] = {
    "backend": "local_spotify_bridge",
    "transport": "spotify_web_api",
    "architecture": {
        "role": "lan_spotify_web_api_proxy",
        "control_plane": "bridge",
        "client_contract": "mqtt_retained_state_and_rpc",
        "recommended_client_transport": "mqtt",
        "on_device_direct_transport": "not_available",
        "direct_spotify_on_device": False,
        "direct_spotify_bridge_proxy": True,
        "direct_spotify_on_device_blocker": "requires_browser_pairing_and_token_storage_hardening",
        "direct_spotify_migration_next": "firmware_token_refresh_and_playback_commands",
        "oauth_owner": "local_bridge",
        "token_storage": "bridge_runtime_or_environment",
        "profile_model": "single_bridge_profile",
        "multi_profile_selection": False,
        "multi_profile_selection_blocker": "profile_registry_not_implemented",
    },
    "runtime_states": [
        "configured",
        "connecting",
        "authenticated",
        "target_ready",
        "command_pending",
        "degraded",
    ],
    "playback": {
        "read_state": True,
        "play_pause": True,
        "next_previous": True,
        "seek": True,
        "shuffle_repeat": True,
        "volume": True,
    },
    "library": {
        "playlists": True,
        "playlist_tracks": True,
        "saved_tracks": True,
        "recent_tracks": True,
    },
    "devices": {
        "list": True,
        "transfer": True,
        "readiness": True,
        "readiness_contract": {
            "safe_for_live_control": "resolved_unrestricted_target",
            "ready_for_live_control": "resolved_unrestricted_active_volume_controllable_nonzero_target",
            "risk_taxonomy": [
                "target_not_configured",
                "devices_not_cached",
                "target_not_found",
                "missing_device_id",
                "restricted_device",
                "inactive_device",
                "volume_unavailable",
                "zero_volume",
            ],
            "muted_or_zero_volume_field": True,
            "last_update_at_field": True,
            "guarded_commands": list(MQTT_READY_TARGET_GUARDED_COMMANDS),
            "explicit_device_id_bypasses_target_gate": True,
            "volume_set_guard": "allowed_to_fix_zero_volume_target",
        },
        "volume_control": True,
    },
    "art": {
        "current": True,
        "adjacent": True,
        "rgb565": True,
    },
}


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
        "capabilities": MQTT_KNOB_BACKEND_CAPABILITIES,
        "limits": {
            "knob_visible_rows": 3,
            "library_page_limit": 3,
            "max_title_chars": 64,
            "max_subtitle_chars": 64,
        },
    }


def mqtt_control_state_payload(
    version: int,
    state: PlaybackSnapshot | None,
    *,
    context_name: str | None = None,
) -> dict[str, Any]:
    context: dict[str, str | None] | None = None
    if state is not None:
        context_parts = playback_context_parts(state)
        display_name = context_name or context_parts["name"]
        if context_parts["type"] != "playlist":
            display_name = context_parts["name"] or state.album
        display_name = display_name or state.album
        context = {
            "type": context_parts["type"],
            "uri": context_parts["uri"],
            "id": context_parts["id"],
            "display_name": display_name,
            "fallback_name": state.album,
        }
    payload: dict[str, Any] = {
        "playing": bool(state.is_playing) if state else False,
        "track_id": state.item_id if state else None,
        "track_uri": state.item_uri if state else None,
        "track_saved": state.item_saved if state else None,
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
        "context": context,
        "shuffle": state.shuffle_state if state else None,
        "repeat": state.repeat_state if state else None,
    }
    return envelope(version=version, payload=payload)
