import hashlib
import json
import time
from typing import Any

from .models import CompactLibraryItem, CompactPage, PlaybackSnapshot
from .store import TargetDevice


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def envelope(*, version: int, payload: dict[str, Any], hash_payload: Any | None = None) -> dict[str, Any]:
    result = dict(payload)
    result["version"] = version
    result["updated_at_ms"] = int(time.time() * 1000)
    result["hash"] = stable_hash(hash_payload if hash_payload is not None else payload)
    return result


def library_root_payload(
    *,
    version: int,
    playlist_total: int | None,
    saved_total: int | None,
    recent_total: int | None,
    device_total: int | None,
) -> dict[str, Any]:
    payload = {
        "pages": [
            {"page": 0, "kind": "playlists", "title": "Playlists", "total": playlist_total, "available": playlist_total is not None},
            {"page": 1, "kind": "saved_tracks", "title": "Saved", "total": saved_total, "available": saved_total is not None},
            {"page": 2, "kind": "recent_tracks", "title": "Recent", "total": recent_total, "available": recent_total is not None},
            {"page": 3, "kind": "devices", "title": "Devices", "total": device_total, "available": device_total is not None},
        ]
    }
    return envelope(version=version, payload=payload)


def library_page_payload(
    *,
    version: int,
    request_id: str | None,
    page: int,
    kind: str,
    title: str,
    compact: CompactPage,
    parent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = [library_item_payload(item, slot) for slot, item in enumerate(compact.items)]
    payload = {
        "request_id": request_id,
        "page": page,
        "kind": kind,
        "parent": parent,
        "title": title,
        "offset": compact.offset,
        "limit": compact.limit,
        "total": compact.total,
        "items": items,
    }
    return envelope(version=version, payload=payload, hash_payload={k: v for k, v in payload.items() if k != "request_id"})


def library_item_payload(item: CompactLibraryItem, slot: int) -> dict[str, Any]:
    item_kind = "track" if item.duration_ms is not None else "playlist"
    subtitle = item.subtitle
    if item_kind == "playlist" and item.track_count is not None:
        subtitle = f"{item.track_count} songs"
    return {
        "slot": slot,
        "id": item.id,
        "uri": item.uri,
        "title": item.title,
        "subtitle": subtitle,
        "image_url": item.image_url,
        "duration_ms": item.duration_ms,
        "track_count": item.track_count,
        "playable": item.playable if item.playable is not None else True,
        "expandable": item_kind == "playlist",
        "item_kind": item_kind,
    }


def devices_payload(
    *,
    version: int,
    request_id: str | None,
    devices: list[dict[str, Any]],
    active_device_id: str | None,
    target: TargetDevice | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    window = devices[offset : offset + limit]
    target_device_id = target.device_id if target else None
    target_device_name = target.device_name if target else None
    items = []
    for slot, device in enumerate(window):
        device_id = device.get("id")
        items.append(
            {
                "slot": slot,
                "id": device_id,
                "name": device.get("name"),
                "type": device.get("type"),
                "is_active": bool(device.get("is_active")),
                "is_target": bool((target_device_id and device_id == target_device_id) or (target_device_name and device.get("name") == target_device_name)),
                "is_restricted": bool(device.get("is_restricted")),
                "can_control_playback": bool(device_id),
                "volume_control_supported": bool(device.get("supports_volume")),
                "volume_percent": device.get("volume_percent"),
            }
        )
    payload = {
        "request_id": request_id,
        "active_device_id": active_device_id,
        "target_device_id": target_device_id,
        "target_device_name": target_device_name,
        "offset": offset,
        "limit": limit,
        "total": len(devices),
        "items": items,
    }
    return envelope(version=version, payload=payload, hash_payload={k: v for k, v in payload.items() if k != "request_id"})


def status_payload(
    *,
    version: int,
    spotify_configured: bool,
    last_poll_at: str | None,
    last_error: str | None,
    current_state: PlaybackSnapshot | None,
    target: TargetDevice | None,
    mqtt_connected: bool,
    command_pulse: dict[str, Any] | None = None,
    target_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_device_id = target.device_id if target else None
    target_device_name = target.device_name if target else None
    active_device_id = current_state.device_id if current_state else None
    target_risks = target_readiness.get("risks", []) if isinstance(target_readiness, dict) else []
    target_safe = bool(target_readiness.get("safe_for_live_control")) if isinstance(target_readiness, dict) else True
    if not spotify_configured:
        status = "spotify_not_configured"
        message = "Pair Spotify in the bridge setup console."
    elif last_error is not None:
        status = "auth_expired" if "401" in last_error or "unauthorized" in last_error.casefold() else "backend_unreachable"
        message = last_error
    elif current_state is None:
        status = "no_active_playback"
        message = "Start Spotify playback on a target device."
    elif target_risks and not target_safe:
        status = "target_not_ready"
        message = "Target device is not ready for live control."
    else:
        status = "ready"
        message = "Ready"
    payload = {
        "status": status,
        "message": message,
        "ok": status == "ready",
        "spotify_configured": spotify_configured,
        "spotify_reachable": spotify_configured and last_error is None,
        "mqtt_connected": mqtt_connected,
        "last_poll_at": last_poll_at,
        "last_error": last_error,
        "target": {
            "device_id": target_device_id,
            "device_name": target_device_name,
            "resolved": bool(target_device_id and active_device_id == target_device_id),
        },
    }
    if target_readiness is not None:
        payload["target_readiness"] = target_readiness
    if command_pulse is not None:
        payload["last_command"] = command_pulse
    hash_payload = {key: value for key, value in payload.items() if key != "last_poll_at"}
    return envelope(version=version, payload=payload, hash_payload=hash_payload)
