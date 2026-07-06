import hashlib
import json
import time
from typing import Any

from .art import ArtOptions, art_version
from .models import PlaybackSnapshot


def knob_snapshot(
    *,
    version: int,
    state: PlaybackSnapshot | None,
    base_url: str,
    spotify_configured: bool,
    art_options: ArtOptions,
    art_hash: str | None = None,
    context_name: str | None = None,
) -> dict[str, Any]:
    updated_at_ms = int(time.time() * 1000)
    if state is None:
        snapshot = {
            "version": version,
            "payload_hash": stable_hash({"state": None}),
            "playback_hash": stable_hash({"playback": None}),
            "art_hash": stable_hash({"art": None}),
            "is_playing": False,
            "progress_ms": None,
            "duration_ms": None,
            "track": None,
            "context": None,
            "device": None,
            "modes": {"shuffle": None, "repeat": None},
            "art": None,
            "server": {
                "ok": True,
                "spotify_configured": spotify_configured,
                "updated_at_ms": updated_at_ms,
            },
        }
        return snapshot

    artist_text = ", ".join(state.artists)
    context = {
        "type": context_type(state),
        "uri": context_uri(state),
        "name": context_name if context_type(state) == "playlist" else None,
        "fallback_name": state.album,
    }
    device = {
        "id": state.device_id,
        "name": state.device_name,
        "type": state.device_type,
        "is_active": state.device_is_active,
        "is_restricted": None,
        "can_control_playback": bool(state.device_id),
        "can_skip_next": bool(state.device_id),
        "can_skip_previous": bool(state.device_id),
        "volume_percent": state.device_volume_percent,
        "volume_control_supported": state.volume_control_supported,
    }
    track = {
        "id": state.item_id,
        "uri": state.item_uri,
        "title": state.title,
        "artists": state.artists,
        "artist_text": artist_text,
        "album": state.album,
    }
    modes = {"shuffle": state.shuffle_state, "repeat": state.repeat_state}
    art = knob_art_payload(state, base_url, art_options)

    playback_hash = stable_hash(
        {
            "is_playing": state.is_playing,
            "progress_ms": state.progress_ms,
            "duration_ms": state.duration_ms,
            "track": track,
            "context": context,
            "device": device,
            "modes": modes,
        }
    )
    final_art_hash = art_hash or stable_hash(art)
    if art is not None:
        art["hash"] = final_art_hash
    payload_hash = stable_hash({"playback_hash": playback_hash, "art_hash": final_art_hash})

    return {
        "version": version,
        "payload_hash": payload_hash,
        "playback_hash": playback_hash,
        "art_hash": final_art_hash,
        "is_playing": state.is_playing,
        "progress_ms": state.progress_ms,
        "duration_ms": state.duration_ms,
        "track": track,
        "context": context,
        "device": device,
        "modes": modes,
        "art": art,
        "server": {
            "ok": True,
            "spotify_configured": spotify_configured,
            "updated_at_ms": updated_at_ms,
        },
    }


def knob_art_payload(
    state: PlaybackSnapshot,
    base_url: str,
    options: ArtOptions,
) -> dict[str, Any] | None:
    if not state.album_art_id:
        return None
    version = art_version(state.album_art_id, options)
    return {
        "id": state.album_art_id,
        "version": version,
        "variant": options.variant,
        "url": (
            f"{base_url.rstrip('/')}/v1/knob/art/current.rgb565"
            f"?size={options.size}&swap={options.swap}&variant={options.variant}"
        ),
        "width": options.size,
        "height": options.size,
        "format": "rgb565",
        "byte_order": options.byte_order,
        "content_length": options.size * options.size * 2,
    }


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def context_type(state: PlaybackSnapshot) -> str | None:
    uri = context_uri(state)
    if not uri:
        return None
    parts = uri.split(":")
    return parts[1] if len(parts) > 1 else None


def context_uri(state: PlaybackSnapshot) -> str | None:
    context = state.raw.get("context") if isinstance(state.raw, dict) else None
    if isinstance(context, dict):
        uri = context.get("uri")
        return uri if isinstance(uri, str) else None
    return None
