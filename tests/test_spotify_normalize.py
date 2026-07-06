from app.spotify import normalize_playback


def test_normalize_current_playback_payload():
    state = normalize_playback(
        {
            "is_playing": True,
            "progress_ms": 1234,
            "shuffle_state": False,
            "repeat_state": "off",
            "device": {"id": "dev1", "name": "Kitchen", "type": "Speaker"},
            "item": {
                "id": "track1",
                "uri": "spotify:track:track1",
                "type": "track",
                "name": "Tune",
                "duration_ms": 180000,
                "artists": [{"name": "Artist"}],
                "album": {
                    "name": "Album",
                    "images": [{"url": "https://example.test/art.jpg"}],
                },
            },
        }
    )

    assert state.is_playing is True
    assert state.title == "Tune"
    assert state.artists == ["Artist"]
    assert state.album_art_url == "https://example.test/art.jpg"
    assert state.device_name == "Kitchen"

