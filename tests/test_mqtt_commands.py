import pytest

from app.mqtt_commands import (
    MQTT_COMMAND_POLICIES,
    MQTT_READY_TARGET_GUARDED_COMMANDS,
    MqttCommandPolicy,
    mqtt_command_policy,
    play_library_item_body,
    playback_body_from_mqtt,
    playlist_id_from_uri,
)
from app.mqtt_contract import MQTT_KNOB_COMMANDS


def test_mqtt_command_policy_marks_playback_followups():
    assert mqtt_command_policy("next").follow_up_refresh is True
    assert mqtt_command_policy("next").playback_affecting is True
    assert mqtt_command_policy("seek").playback_affecting is True
    assert mqtt_command_policy("shuffle_set").playback_affecting is True
    assert mqtt_command_policy("repeat_set").playback_affecting is True
    assert mqtt_command_policy("transfer").refresh_devices is True
    assert mqtt_command_policy("volume_set").follow_up_refresh is True
    assert mqtt_command_policy("unknown").follow_up_refresh is False


def test_advertised_mqtt_commands_have_explicit_policies():
    assert set(MQTT_KNOB_COMMANDS) == set(MQTT_COMMAND_POLICIES)
    for command in MQTT_KNOB_COMMANDS:
        assert isinstance(mqtt_command_policy(command), MqttCommandPolicy)


def test_ready_target_guarded_commands_are_policy_backed():
    assert "volume_set" not in MQTT_READY_TARGET_GUARDED_COMMANDS
    assert "transfer" not in MQTT_READY_TARGET_GUARDED_COMMANDS
    for command in MQTT_READY_TARGET_GUARDED_COMMANDS:
        assert command in MQTT_COMMAND_POLICIES
        assert mqtt_command_policy(command).playback_affecting is True


def test_playback_body_from_mqtt_maps_play_fields():
    assert playback_body_from_mqtt(
        {
            "uri": "spotify:playlist:abc",
            "position_ms": 500,
            "offset": {"position": 1},
        }
    ) == {
        "context_uri": "spotify:playlist:abc",
        "position_ms": 500,
        "offset": {"position": 1},
    }


def test_play_library_item_body_supports_context_and_saved_tracks():
    assert play_library_item_body(
        {
            "context_uri": "spotify:playlist:abc",
            "item_uri": "spotify:track:1",
        }
    ) == {
        "context_uri": "spotify:playlist:abc",
        "offset": {"uri": "spotify:track:1"},
    }
    assert play_library_item_body(
        {
            "source_kind": "saved_tracks",
            "item_uri": "spotify:track:2",
        }
    ) == {"uris": ["spotify:track:2"]}
    assert play_library_item_body(
        {
            "source_kind": "recent_tracks",
            "item_uri": "spotify:track:3",
        }
    ) == {"uris": ["spotify:track:3"]}


def test_play_library_item_body_rejects_missing_target():
    with pytest.raises(ValueError, match="play_library_item requires"):
        play_library_item_body({"type": "play_library_item"})


def test_playlist_id_from_uri():
    assert playlist_id_from_uri("spotify:playlist:abc") == "abc"
    assert playlist_id_from_uri("spotify:track:abc") is None
    assert playlist_id_from_uri(None) is None
