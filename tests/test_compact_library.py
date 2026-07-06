from app.spotify import compact_playlists, compact_tracks


def test_compact_playlists_keeps_knob_fields_only():
    page = compact_playlists(
        {
            "limit": 50,
            "offset": 0,
            "total": 1,
            "next": None,
            "items": [
                {
                    "id": "playlist-1",
                    "uri": "spotify:playlist:playlist-1",
                    "name": "Favorites",
                    "images": [{"url": "https://example.test/playlist.jpg"}],
                    "owner": {"display_name": "Gabriel"},
                    "tracks": {"total": 12},
                }
            ],
        }
    )

    assert page.items[0].title == "Favorites"
    assert page.items[0].subtitle == "Gabriel"
    assert page.items[0].track_count == 12
    assert page.items[0].image_url == "https://example.test/playlist.jpg"


def test_compact_tracks_handles_playlist_track_wrappers():
    page = compact_tracks(
        {
            "limit": 100,
            "offset": 0,
            "total": 1,
            "next": None,
            "items": [
                {
                    "track": {
                        "id": "track-1",
                        "uri": "spotify:track:track-1",
                        "name": "Tune",
                        "duration_ms": 180000,
                        "explicit": False,
                        "is_playable": True,
                        "artists": [{"name": "Artist"}],
                        "album": {
                            "name": "Album",
                            "images": [{"url": "https://example.test/album.jpg"}],
                        },
                    }
                }
            ],
        }
    )

    assert page.items[0].title == "Tune"
    assert page.items[0].subtitle == "Artist"
    assert page.items[0].duration_ms == 180000
    assert page.items[0].playable is True
