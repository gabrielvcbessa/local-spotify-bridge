from app.spotify import normalize_playback


def test_normalize_current_playback_payload():
    state = normalize_playback(
        {
            "is_playing": True,
            "progress_ms": 1234,
            "shuffle_state": False,
            "repeat_state": "off",
            "device": {
                "id": "dev1",
                "name": "Kitchen",
                "type": "Speaker",
                "is_active": True,
                "volume_percent": 42,
                "supports_volume": True,
            },
            "item": {
                "id": "track1",
                "uri": "spotify:track:track1",
                "type": "track",
                "name": "Tune",
                "duration_ms": 180000,
                "artists": [{"name": "Artist"}],
                "album": {
                    "name": "Album",
                    "images": [
                        {
                            "url": "https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271"
                        }
                    ],
                },
            },
        }
    )

    assert state.is_playing is True
    assert state.title == "Tune"
    assert state.artists == ["Artist"]
    assert state.album_art_url == "https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271"
    assert state.album_art_id == "ab67616d0000b273adfc1ac5836f96adac580271"
    assert state.knob_art_version == "ab67616d0000b273adfc1ac5836f96adac580271"
    assert state.device_name == "Kitchen"
    assert state.device_is_active is True
    assert state.device_volume_percent == 42
    assert state.volume_control_supported is True


def test_normalize_defaults_volume_control_to_false_when_unknown():
    state = normalize_playback(
        {
            "is_playing": True,
            "device": {"id": "dev1", "name": "Kitchen", "type": "Speaker"},
            "item": {"id": "track1", "name": "Tune"},
        }
    )

    assert state.volume_control_supported is False
