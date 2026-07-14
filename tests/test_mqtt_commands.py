import pytest

from app.mqtt_commands import (
    mqtt_command_policy,
    play_library_item_body,
    playback_body_from_mqtt,
    playlist_id_from_uri,
)


def test_mqtt_command_policy_marks_playback_followups():
    assert mqtt_command_policy("next").follow_up_refresh is True
    assert mqtt_command_policy("next").playback_affecting is True
    assert mqtt_command_policy("seek").playback_affecting is True
    assert mqtt_command_policy("shuffle_set").playback_affecting is True
    assert mqtt_command_policy("repeat_set").playback_affecting is True
    assert mqtt_command_policy("transfer").refresh_devices is True
    assert mqtt_command_policy("volume_set").follow_up_refresh is True
    assert mqtt_command_policy("unknown").follow_up_refresh is False


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
