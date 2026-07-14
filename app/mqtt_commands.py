from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MqttCommandPolicy:
    follow_up_refresh: bool = False
    refresh_devices: bool = False
    playback_affecting: bool = False


MQTT_COMMAND_POLICIES: dict[str, MqttCommandPolicy] = {
    "play_pause": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
    "play": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
    "pause": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
    "next": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
    "previous": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
    "volume_set": MqttCommandPolicy(),
    "seek": MqttCommandPolicy(),
    "select_source": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
    "transfer": MqttCommandPolicy(follow_up_refresh=True, refresh_devices=True, playback_affecting=True),
    "shuffle_set": MqttCommandPolicy(),
    "repeat_set": MqttCommandPolicy(),
    "save_current_track": MqttCommandPolicy(follow_up_refresh=True),
    "unsave_current_track": MqttCommandPolicy(follow_up_refresh=True),
    "play_library_item": MqttCommandPolicy(follow_up_refresh=True, playback_affecting=True),
}


def mqtt_command_policy(command_type: str) -> MqttCommandPolicy:
    return MQTT_COMMAND_POLICIES.get(command_type, MqttCommandPolicy())


def playback_body_from_mqtt(command: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for source, target in (
        ("context_uri", "context_uri"),
        ("uri", "context_uri"),
        ("uris", "uris"),
        ("offset", "offset"),
        ("position_ms", "position_ms"),
    ):
        if command.get(source) is not None:
            body[target] = command[source]
    return body


def play_library_item_body(command: dict[str, Any]) -> dict[str, Any]:
    context_uri = command.get("context_uri")
    item_uri = command.get("item_uri")
    source_kind = command.get("source_kind")
    if isinstance(context_uri, str) and context_uri:
        offset = command.get("offset")
        body: dict[str, Any] = {"context_uri": context_uri}
        if isinstance(offset, dict):
            body["offset"] = offset
        elif isinstance(item_uri, str) and item_uri:
            body["offset"] = {"uri": item_uri}
        if isinstance(command.get("position_ms"), int):
            body["position_ms"] = command["position_ms"]
        return body

    if source_kind in {"saved_tracks", "recent_tracks"}:
        uris = command.get("uris")
        if isinstance(uris, list) and all(isinstance(uri, str) for uri in uris):
            body = {"uris": uris}
            if isinstance(item_uri, str) and item_uri in uris:
                body["offset"] = {"position": uris.index(item_uri)}
            return body
        if isinstance(item_uri, str) and item_uri:
            return {"uris": [item_uri]}

    raise ValueError("play_library_item requires context_uri or saved_tracks item_uri.")


def playlist_id_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    parts = uri.split(":")
    if len(parts) != 3 or parts[0] != "spotify" or parts[1] != "playlist" or not parts[2]:
        return None
    return parts[2]
