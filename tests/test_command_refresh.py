import pytest

import app.main as main
from app.models import PlaybackCommand, PlaybackSnapshot, SeekCommand, TargetDeviceCommand, TransferPlaybackCommand, VolumeCommand


class FakeSpotifyClient:
    def __init__(self) -> None:
        self.calls = 0

    async def current_playback(self) -> PlaybackSnapshot:
        self.calls += 1
        return PlaybackSnapshot(title=f"Song {self.calls}")


class FakeCommandSpotifyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.spotify_configured = True
        self.device_items: list[dict] = [
            {"id": "speaker-1", "name": "Speaker 1", "is_active": True, "supports_volume": True},
            {"id": "speaker-2", "name": "Speaker 2", "is_active": False, "supports_volume": False},
        ]

    async def play(self, body=None, device_id: str | None = None) -> None:
        self.calls.append(("play", device_id))

    async def pause(self, device_id: str | None = None) -> None:
        self.calls.append(("pause", device_id))

    async def next_track(self, device_id: str | None = None) -> None:
        self.calls.append(("next", device_id))

    async def previous_track(self, device_id: str | None = None) -> None:
        self.calls.append(("previous", device_id))

    async def seek(self, position_ms: int, device_id: str | None = None) -> None:
        self.calls.append(("seek", device_id))

    async def set_volume(self, volume_percent: int, device_id: str | None = None) -> None:
        self.calls.append(("volume", device_id))

    async def set_shuffle(self, enabled: bool, device_id: str | None = None) -> None:
        self.calls.append(("shuffle", device_id))

    async def set_repeat(self, mode: str, device_id: str | None = None) -> None:
        self.calls.append(("repeat", device_id))

    async def transfer_playback(self, device_id: str, play: bool = True) -> None:
        self.calls.append(("transfer", device_id))

    async def save_track(self, track_id: str) -> None:
        self.calls.append(("save_track", track_id))

    async def remove_saved_track(self, track_id: str) -> None:
        self.calls.append(("remove_saved_track", track_id))

    async def devices(self):
        return {"devices": self.device_items}


class FakeDevicesClient:
    def __init__(self) -> None:
        self.devices_calls = 0
        self.playlists_calls = 0
        self.saved_tracks_calls = 0
        self.recent_tracks_calls = 0

    async def devices(self):
        self.devices_calls += 1
        return {
            "devices": [
                {
                    "id": "device-1",
                    "name": "Speaker",
                    "type": "Speaker",
                    "is_active": True,
                    "supports_volume": True,
                    "volume_percent": 42,
                }
            ]
        }

    async def playlists(self, *, limit: int, offset: int):
        self.playlists_calls += 1
        items = [
            {
                "id": "playlist-b",
                "uri": "spotify:playlist:playlist-b",
                "name": "Beta",
                "owner": {"display_name": "Gabriel"},
                "tracks": {"total": 2},
            },
            {
                "id": "playlist-a",
                "uri": "spotify:playlist:playlist-a",
                "name": "Alpha",
                "owner": {"display_name": "Gabriel"},
                "tracks": {"total": 1},
            },
            {
                "id": "playlist-c",
                "uri": "spotify:playlist:playlist-c",
                "name": "charlie",
                "owner": {"display_name": "Gabriel"},
                "tracks": {"total": 3},
            },
        ]
        page_items = items[offset : offset + 2]
        next_url = "https://api.spotify.com/v1/me/playlists?offset=2" if offset + len(page_items) < len(items) else None
        return {"total": len(items), "limit": limit, "offset": offset, "next": next_url, "items": page_items}

    async def saved_tracks(self, *, limit: int, offset: int):
        self.saved_tracks_calls += 1
        return {"total": 5, "items": []}

    async def recently_played_tracks(self, *, limit: int):
        self.recent_tracks_calls += 1
        return {
            "items": [
                {
                    "track": {
                        "id": "recent-1",
                        "uri": "spotify:track:recent-1",
                        "name": "Recent One",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Album", "images": []},
                        "duration_ms": 123000,
                    }
                }
            ][:limit]
        }


@pytest.mark.asyncio
async def test_refresh_and_publish_schedules_follow_up_refreshes(monkeypatch):
    client = FakeSpotifyClient()
    published: list[str | None] = []
    scheduled = []

    async def fake_publish_if_changed(state, *, force=False):
        published.append(state.title if state else None)
        published.append(f"force={force}")
        return True

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(main.broker, "publish_if_changed", fake_publish_if_changed)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    did_publish = await main.refresh_and_publish(client, follow_up_delays=(0.5, 1.5))

    assert published == ["Song 1", "force=False"]
    assert len(scheduled) == 2
    assert did_publish is True


@pytest.mark.asyncio
async def test_refresh_after_successful_command_forces_state_publish(monkeypatch):
    client = FakeSpotifyClient()
    calls = []

    async def fake_publish_if_changed(state, *, force=False):
        calls.append((state.title if state else None, force))
        return True

    monkeypatch.setattr(main.broker, "publish_if_changed", fake_publish_if_changed)

    did_publish = await main.refresh_after_successful_command(client)

    assert calls == [("Song 1", True)]
    assert did_publish is True


@pytest.mark.asyncio
async def test_mqtt_knob_snapshot_publishes_control_state(monkeypatch):
    published = []
    status_forces = []

    async def fake_publish_mqtt_retained(topic, payload, *, force=False):
        published.append((topic, payload, force))

    async def fake_publish_mqtt_art_payloads(_client, _state, _options):
        return None

    async def fake_publish_mqtt_status(
        command_type=None,
        command_request_id=None,
        command_pending=None,
        command_ok=None,
        command_error=None,
        force_publish=False,
    ):
        _ = (command_type, command_request_id, command_pending, command_ok)
        status_forces.append(force_publish)
        return None

    async def fake_prewarm_cached_track_art(*_args):
        return None

    async def fake_resolved_context_name(*_args, **_kwargs):
        return "Resolved Playlist"

    monkeypatch.setattr(main.broker, "publish_mqtt_retained", fake_publish_mqtt_retained)
    monkeypatch.setattr(main, "publish_mqtt_art_payloads", fake_publish_mqtt_art_payloads)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)
    monkeypatch.setattr(main, "prewarm_cached_track_art", fake_prewarm_cached_track_art)
    monkeypatch.setattr(main, "resolved_context_name", fake_resolved_context_name)

    state = PlaybackSnapshot(
        is_playing=True,
        item_id="track-1",
        item_uri="spotify:track:track-1",
        title="Song",
        artists=["Artist"],
        device_id="device-1",
        device_name="Speaker",
        device_volume_percent=42,
        volume_control_supported=True,
        album="Album fallback",
        raw={"context": {"type": "playlist", "uri": "spotify:playlist:playlist-1"}},
    )

    await main.mqtt_knob_snapshot(7, state, force_publish=True)

    control = [(payload, force) for topic, payload, force in published if topic == "control_state"]
    assert len(control) == 1
    payload, force = control[0]
    assert force is True
    assert status_forces == [True]
    assert payload["version"] == 7
    assert payload["playing"] is True
    assert payload["track_id"] == "track-1"
    assert payload["context"]["display_name"] == "Resolved Playlist"
    assert payload["device"]["id"] == "device-1"
    assert payload["device"]["volume_percent"] == 42


@pytest.mark.asyncio
async def test_refresh_devices_and_publish_updates_cache_and_retained_topic(monkeypatch):
    client = FakeDevicesClient()
    published = []
    previous_cached = main.cached_devices

    async def fake_publish_mqtt_retained(topic, payload):
        published.append((topic, payload))

    monkeypatch.setattr(main.broker, "publish_mqtt_retained", fake_publish_mqtt_retained)
    main.cached_devices = None
    try:
        payload = await main.refresh_devices_and_publish(client)
        cached = main.cached_devices
    finally:
        main.cached_devices = previous_cached

    assert client.devices_calls == 1
    assert cached is not None
    assert cached[0]["id"] == "device-1"
    assert payload["items"][0]["id"] == "device-1"
    assert published == [("devices", payload)]


@pytest.mark.asyncio
async def test_library_root_uses_cached_devices_without_fetching_devices():
    client = FakeDevicesClient()
    previous_cached = main.cached_devices
    main.cached_devices = [{"id": "cached-device"}]
    try:
        payload = await main.build_library_root_payload(client)
    finally:
        main.cached_devices = previous_cached

    assert client.devices_calls == 0
    assert payload["pages"][2]["kind"] == "recent_tracks"
    assert payload["pages"][2]["total"] == 1
    assert payload["pages"][3]["total"] == 1


@pytest.mark.asyncio
async def test_recent_tracks_library_page_uses_recent_window():
    client = FakeDevicesClient()

    payload = await main.build_library_page_payload(
        client,
        request_id="recent-1",
        page=2,
        kind="recent_tracks",
        offset=0,
        limit=3,
    )

    assert payload["request_id"] == "recent-1"
    assert payload["kind"] == "recent_tracks"
    assert payload["title"] == "Recent"
    assert payload["items"][0]["uri"] == "spotify:track:recent-1"
    assert payload["items"][0]["item_kind"] == "track"


@pytest.mark.asyncio
async def test_full_playlists_payload_paginates_all_playlists_in_spotify_order(monkeypatch):
    client = FakeDevicesClient()
    previous_sort = main.settings.spotify_playlist_sort
    monkeypatch.setattr(main.settings, "spotify_playlist_sort", "spotify")

    payload = await main.build_full_playlists_payload(client)

    monkeypatch.setattr(main.settings, "spotify_playlist_sort", previous_sort)
    assert client.playlists_calls == 2
    assert payload["total"] == 3
    assert [item["title"] for item in payload["items"]] == ["Beta", "Alpha", "charlie"]
    assert payload["sort_order"] == "spotify"


@pytest.mark.asyncio
async def test_full_playlists_payload_can_sort_alphabetically(monkeypatch):
    client = FakeDevicesClient()
    previous_sort = main.settings.spotify_playlist_sort
    monkeypatch.setattr(main.settings, "spotify_playlist_sort", "alpha")

    payload = await main.build_full_playlists_payload(client)

    monkeypatch.setattr(main.settings, "spotify_playlist_sort", previous_sort)
    assert client.playlists_calls == 2
    assert [item["title"] for item in payload["items"]] == ["Alpha", "Beta", "charlie"]
    assert payload["sort_order"] == "alpha"


@pytest.mark.asyncio
async def test_mqtt_next_previous_do_not_use_implicit_target_device(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.handle_mqtt_command({"request_id": "knob-next-1", "type": "next"})
    await main.handle_mqtt_command({"request_id": "knob-prev-1", "type": "previous"})
    await main.handle_mqtt_command({"type": "next", "device_id": "speaker-1"})

    assert client.calls == [
        ("next", None),
        ("previous", None),
        ("next", "speaker-1"),
    ]
    assert refreshes == [
        main.settings.command_followup_refresh_delays_for("next"),
        main.settings.command_followup_refresh_delays_for("previous"),
        main.settings.command_followup_refresh_delays_for("next"),
    ]
    assert status_pulses == [
        ("next", "knob-next-1", True, None),
        ("next", "knob-next-1", False, True),
        ("previous", "knob-prev-1", True, None),
        ("previous", "knob-prev-1", False, True),
        ("next", None, True, None),
        ("next", None, False, True),
    ]


@pytest.mark.asyncio
async def test_mqtt_transfer_refreshes_devices_and_state(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    device_refreshes = []
    events = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        events.append(("refresh", tuple(follow_up_delays), force_publish))
        refreshes.append(tuple(follow_up_delays))
        return True

    async def fake_refresh_devices_and_publish(_client):
        events.append(("devices", _client))
        device_refreshes.append(True)
        return {"items": []}

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        events.append(("status", command_type, command_request_id, command_pending, command_ok))
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "refresh_devices_and_publish", fake_refresh_devices_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)
    monkeypatch.setattr(main.store, "set_target_device", lambda _target: None)

    await main.handle_mqtt_command(
        {"request_id": "knob-transfer-1", "type": "transfer", "device_id": "speaker-1", "set_target": True}
    )

    assert client.calls == [("transfer", "speaker-1")]
    assert device_refreshes == [True]
    assert refreshes == [main.settings.command_followup_refresh_delays_for("transfer")]
    assert status_pulses == [("transfer", "knob-transfer-1", True, None), ("transfer", "knob-transfer-1", False, True)]
    assert events == [
        ("status", "transfer", "knob-transfer-1", True, None),
        ("status", "transfer", "knob-transfer-1", False, True),
        ("devices", client),
        ("refresh", main.settings.command_followup_refresh_delays_for("transfer"), True),
    ]


@pytest.mark.asyncio
async def test_mqtt_transfer_success_survives_device_refresh_failure(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []
    previous_error = main.broker.last_spotify_error

    async def fake_refresh_devices_and_publish(_client):
        raise RuntimeError("devices unavailable")

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        return True

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_devices_and_publish", fake_refresh_devices_and_publish)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)
    monkeypatch.setattr(main.store, "set_target_device", lambda _target: None)

    try:
        result = await main.handle_mqtt_command(
            {"request_id": "knob-transfer-device-refresh-fail", "type": "transfer", "device_id": "speaker-1", "set_target": True}
        )
        assert main.broker.last_spotify_error == "devices unavailable"
    finally:
        main.broker.last_spotify_error = previous_error

    assert result["published_state"] is True
    assert result["state_refresh_ok"] is True
    assert result["state_publish_forced"] is True
    assert client.calls == [("transfer", "speaker-1")]
    assert status_pulses == [
        ("transfer", "knob-transfer-device-refresh-fail", True, None),
        ("transfer", "knob-transfer-device-refresh-fail", False, True),
    ]


@pytest.mark.asyncio
async def test_mqtt_command_success_status_publishes_before_state_refresh(monkeypatch):
    client = FakeCommandSpotifyClient()
    events = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        events.append(("refresh", tuple(follow_up_delays), force_publish))
        return True

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        events.append(("status", command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.handle_mqtt_command({"request_id": "knob-next-ack", "type": "next"})

    assert events == [
        ("status", "next", "knob-next-ack", True, None),
        ("status", "next", "knob-next-ack", False, True),
        ("refresh", main.settings.command_followup_refresh_delays_for("next"), True),
    ]


@pytest.mark.asyncio
async def test_mqtt_fast_controls_use_command_follow_up_profiles(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        refreshes.append((tuple(follow_up_delays), force_publish))
        return True

    async def fake_command_device_id(_client, device_id):
        return device_id

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        _ = command_error
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "command_device_id", fake_command_device_id)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.handle_mqtt_command(
        {"request_id": "knob-volume-1", "type": "volume_set", "volume_percent": 42, "device_id": "speaker-1"}
    )
    await main.handle_mqtt_command(
        {"request_id": "knob-seek-1", "type": "seek", "position_ms": 30000, "device_id": "speaker-1"}
    )
    await main.handle_mqtt_command(
        {"request_id": "knob-shuffle-1", "type": "shuffle_set", "enabled": True, "device_id": "speaker-1"}
    )
    await main.handle_mqtt_command(
        {"request_id": "knob-repeat-1", "type": "repeat_set", "mode": "context", "device_id": "speaker-1"}
    )

    assert client.calls == [
        ("volume", "speaker-1"),
        ("seek", "speaker-1"),
        ("shuffle", "speaker-1"),
        ("repeat", "speaker-1"),
    ]
    assert refreshes == [
        (main.settings.command_followup_refresh_delays_for("volume_set"), True),
        (main.settings.command_followup_refresh_delays_for("seek"), True),
        (main.settings.command_followup_refresh_delays_for("shuffle_set"), True),
        (main.settings.command_followup_refresh_delays_for("repeat_set"), True),
    ]
    assert status_pulses == [
        ("volume_set", "knob-volume-1", True, None),
        ("volume_set", "knob-volume-1", False, True),
        ("seek", "knob-seek-1", True, None),
        ("seek", "knob-seek-1", False, True),
        ("shuffle_set", "knob-shuffle-1", True, None),
        ("shuffle_set", "knob-shuffle-1", False, True),
        ("repeat_set", "knob-repeat-1", True, None),
        ("repeat_set", "knob-repeat-1", False, True),
    ]


@pytest.mark.asyncio
async def test_mqtt_command_success_survives_refresh_failure(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []
    previous_error = main.broker.last_spotify_error

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        raise RuntimeError("refresh unavailable")

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    try:
        result = await main.handle_mqtt_command({"request_id": "knob-next-refresh-fail", "type": "next"})
        assert main.broker.last_spotify_error == "refresh unavailable"
    finally:
        main.broker.last_spotify_error = previous_error

    assert result["published_state"] is False
    assert result["state_refresh_ok"] is False
    assert result["state_publish_forced"] is True
    assert result["playback_affecting"] is True
    assert status_pulses == [
        ("next", "knob-next-refresh-fail", True, None),
        ("next", "knob-next-refresh-fail", False, True),
    ]


@pytest.mark.asyncio
async def test_mqtt_save_track_uses_payload_track_uri_and_publishes_status(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.handle_mqtt_command(
        {
            "request_id": "knob-like-1",
            "type": "save_current_track",
            "track_uri": "spotify:track:track-1",
        }
    )

    assert client.calls == [("save_track", "track-1")]
    assert refreshes == [main.settings.command_followup_refresh_delays_for("save_current_track")]
    assert status_pulses == [
        ("save_current_track", "knob-like-1", True, None),
        ("save_current_track", "knob-like-1", False, True),
    ]


@pytest.mark.asyncio
async def test_mqtt_unsave_track_falls_back_to_current_state(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    main.broker.current_state = PlaybackSnapshot(item_id="track-2")
    try:
        await main.handle_mqtt_command({"request_id": "knob-unlike-1", "type": "unsave_current_track"})
    finally:
        main.broker.current_state = None

    assert client.calls == [("remove_saved_track", "track-2")]
    assert refreshes == [main.settings.command_followup_refresh_delays_for("unsave_current_track")]
    assert status_pulses == [
        ("unsave_current_track", "knob-unlike-1", True, None),
        ("unsave_current_track", "knob-unlike-1", False, True),
    ]


@pytest.mark.asyncio
async def test_mqtt_play_pause_does_not_use_implicit_target_device(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        _ = (command_type, command_request_id, command_pending, command_ok)
        return None

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    main.broker.current_state = PlaybackSnapshot(is_playing=True)
    try:
        await main.handle_mqtt_command({"type": "play_pause"})
        main.broker.current_state = PlaybackSnapshot(is_playing=False)
        await main.handle_mqtt_command({"type": "play_pause"})
        await main.handle_mqtt_command({"type": "pause", "device_id": "speaker-1"})
    finally:
        main.broker.current_state = None

    assert client.calls == [
        ("pause", None),
        ("play", None),
        ("pause", "speaker-1"),
    ]
    assert refreshes == [
        main.settings.command_followup_refresh_delays_for("play_pause"),
        main.settings.command_followup_refresh_delays_for("play_pause"),
        main.settings.command_followup_refresh_delays_for("pause"),
    ]


@pytest.mark.asyncio
async def test_rest_controls_use_command_specific_follow_up_profiles(monkeypatch):
    client = FakeCommandSpotifyClient()
    events = []
    refreshes = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        events.append(("refresh", tuple(follow_up_delays)))
        refreshes.append(tuple(follow_up_delays))

    async def fake_command_device_id(_client, device_id):
        return device_id

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        _ = (command_pending, command_ok)
        events.append(("status", command_type, command_request_id))
        status_pulses.append((command_type, command_request_id))

    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "command_device_id", fake_command_device_id)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.play(PlaybackCommand(device_id="speaker-1"), client=client)
    await main.pause(device_id="speaker-2", client=client)
    await main.next_track(client=client)
    await main.previous_track(client=client)

    assert client.calls == [
        ("play", "speaker-1"),
        ("pause", "speaker-2"),
        ("next", None),
        ("previous", None),
    ]
    assert refreshes == [
        main.settings.command_followup_refresh_delays_for("play"),
        main.settings.command_followup_refresh_delays_for("pause"),
        main.settings.command_followup_refresh_delays_for("next"),
        main.settings.command_followup_refresh_delays_for("previous"),
    ]
    assert status_pulses == [("play", None), ("pause", None), ("next", None), ("previous", None)]
    assert events == [
        ("status", "play", None),
        ("refresh", main.settings.command_followup_refresh_delays_for("play")),
        ("status", "pause", None),
        ("refresh", main.settings.command_followup_refresh_delays_for("pause")),
        ("status", "next", None),
        ("refresh", main.settings.command_followup_refresh_delays_for("next")),
        ("status", "previous", None),
        ("refresh", main.settings.command_followup_refresh_delays_for("previous")),
    ]


@pytest.mark.asyncio
async def test_rest_seek_and_volume_publish_status_before_refresh(monkeypatch):
    client = FakeCommandSpotifyClient()
    events = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        events.append(("refresh", tuple(follow_up_delays)))

    async def fake_command_device_id(_client, device_id):
        return device_id

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        _ = (command_request_id, command_pending)
        events.append(("status", command_type, command_ok))

    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "command_device_id", fake_command_device_id)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.seek(SeekCommand(position_ms=42000, device_id="speaker-1"), client=client)
    await main.set_volume(VolumeCommand(volume_percent=42, device_id="speaker-2"), client=client)

    assert client.calls == [("seek", "speaker-1"), ("volume", "speaker-2")]
    assert events == [
        ("status", "seek", True),
        ("refresh", ()),
        ("status", "volume_set", True),
        ("refresh", ()),
    ]


@pytest.mark.asyncio
async def test_rest_control_success_survives_refresh_failure(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []
    previous_error = main.broker.last_spotify_error

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        raise RuntimeError("rest refresh unavailable")

    async def fake_command_device_id(_client, device_id):
        return device_id

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "command_device_id", fake_command_device_id)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    try:
        await main.play(PlaybackCommand(device_id="speaker-1"), client=client)
        assert main.broker.last_spotify_error == "rest refresh unavailable"
    finally:
        main.broker.last_spotify_error = previous_error

    assert client.calls == [("play", "speaker-1")]
    assert status_pulses == [("play", None, None, True)]


@pytest.mark.asyncio
async def test_rest_transfer_paths_use_transfer_follow_up_profile(monkeypatch):
    client = FakeCommandSpotifyClient()
    events = []
    refreshes = []
    device_refreshes = []
    targets = []
    status_pulses = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        events.append(("refresh", tuple(follow_up_delays)))
        refreshes.append(tuple(follow_up_delays))

    async def fake_refresh_devices_and_publish(_client):
        events.append(("devices", _client))
        device_refreshes.append(_client)
        return {"items": []}

    async def fake_resolve_target_device_id(*_args, **_kwargs):
        return "speaker-1"

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        _ = (command_pending, command_ok)
        events.append(("status", command_type, command_request_id))
        status_pulses.append((command_type, command_request_id))

    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "refresh_devices_and_publish", fake_refresh_devices_and_publish)
    monkeypatch.setattr(client, "spotify_configured", True, raising=False)
    monkeypatch.setattr(client, "resolve_target_device_id", fake_resolve_target_device_id, raising=False)
    monkeypatch.setattr(main.store, "set_target_device", lambda target: (targets.append(target), events.append(("target", target))))
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.set_target(
        TargetDeviceCommand(device_id="speaker-1", transfer_playback=True),
        client=client,
        state_store=main.store,
    )
    await main.transfer_playback(TransferPlaybackCommand(device_id="speaker-2"), client=client)

    assert client.calls == [
        ("transfer", "speaker-1"),
        ("transfer", "speaker-2"),
    ]
    assert refreshes == [
        main.settings.command_followup_refresh_delays_for("transfer"),
        main.settings.command_followup_refresh_delays_for("transfer"),
    ]
    assert device_refreshes == [client, client]
    assert status_pulses == [("transfer", None), ("transfer", None)]
    assert events == [
        ("target", main.TargetDevice(device_id="speaker-1", device_name=None)),
        ("status", "transfer", None),
        ("refresh", main.settings.command_followup_refresh_delays_for("transfer")),
        ("devices", client),
        ("target", main.TargetDevice(device_id="speaker-2", device_name=None)),
        ("status", "transfer", None),
        ("refresh", main.settings.command_followup_refresh_delays_for("transfer")),
        ("devices", client),
    ]


@pytest.mark.asyncio
async def test_rest_transfer_success_survives_device_refresh_failure(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []
    previous_error = main.broker.last_spotify_error
    targets = []

    async def fake_refresh_devices_and_publish(_client):
        raise RuntimeError("rest devices unavailable")

    async def fake_refresh_and_publish(_client, *, follow_up_delays=(), force_publish=False):
        return None

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok))

    monkeypatch.setattr(main, "refresh_devices_and_publish", fake_refresh_devices_and_publish)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main.store, "set_target_device", lambda target: targets.append(target))
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    try:
        await main.transfer_playback(TransferPlaybackCommand(device_id="speaker-1"), client=client)
        assert main.broker.last_spotify_error == "rest devices unavailable"
    finally:
        main.broker.last_spotify_error = previous_error

    assert client.calls == [("transfer", "speaker-1")]
    assert [target.device_id for target in targets] == ["speaker-1"]
    assert status_pulses == [("transfer", None, None, True)]


@pytest.mark.asyncio
async def test_rest_set_target_refreshes_devices_before_status(monkeypatch):
    client = FakeCommandSpotifyClient()
    events = []

    async def fake_refresh_devices_and_publish(_client):
        events.append(("devices", _client))
        return {"items": []}

    async def fake_publish_mqtt_status(
        command_type=None,
        command_request_id=None,
        command_pending=None,
        command_ok=None,
        command_error=None,
        force_publish=False,
    ):
        _ = (command_request_id, command_pending)
        events.append(("status", command_type, force_publish))

    monkeypatch.setattr(client, "spotify_configured", True, raising=False)
    monkeypatch.setattr(main, "refresh_devices_and_publish", fake_refresh_devices_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)
    monkeypatch.setattr(main.store, "set_target_device", lambda target: events.append(("target", target)))

    response = await main.set_target(
        TargetDeviceCommand(device_id="speaker-1"),
        client=client,
        state_store=main.store,
    )

    assert response["resolved_device_id"] == "speaker-1"
    assert events == [
        ("target", main.TargetDevice(device_id="speaker-1", device_name=None)),
        ("devices", client),
        ("status", "set_target", True),
    ]


@pytest.mark.asyncio
async def test_target_device_readiness_reports_risks():
    client = FakeCommandSpotifyClient()
    client.device_items = [
        {"id": "speaker-1", "name": "Speaker 1", "is_active": False, "supports_volume": False},
        {"id": "restricted-1", "name": "Restricted", "is_restricted": True, "supports_volume": True},
    ]

    readiness = await main.target_device_readiness(
        client,
        main.TargetDevice(device_id="speaker-1"),
        refresh=True,
    )
    restricted = await main.target_device_readiness(
        client,
        main.TargetDevice(device_id="restricted-1"),
        refresh=True,
    )
    missing = await main.target_device_readiness(
        client,
        main.TargetDevice(device_id="missing"),
        refresh=True,
    )

    assert readiness["safe_for_live_control"] is True
    assert readiness["ready_for_live_control"] is False
    assert readiness["active"] is False
    assert readiness["volume_control_supported"] is False
    assert readiness["muted_or_zero_volume"] is False
    assert readiness["resolved_device_id"] == "speaker-1"
    assert readiness["risks"] == ["inactive_device", "volume_unavailable"]
    assert restricted["safe_for_live_control"] is False
    assert restricted["ready_for_live_control"] is False
    assert "restricted_device" in restricted["risks"]
    assert missing["safe_for_live_control"] is False
    assert missing["ready_for_live_control"] is False
    assert "target_not_found" in missing["risks"]


def test_cached_target_readiness_reports_unavailable_without_device_cache(monkeypatch):
    previous_cached = main.cached_devices
    main.cached_devices = None
    try:
        readiness = main.cached_target_readiness(main.TargetDevice(device_id="speaker-1"))
    finally:
        main.cached_devices = previous_cached

    assert readiness["source"] == "unavailable"
    assert readiness["safe_for_live_control"] is False
    assert readiness["ready_for_live_control"] is False
    assert readiness["last_update_at"] == readiness["checked_at"]
    assert readiness["risks"] == ["devices_not_cached"]


def test_mqtt_status_payload_uses_cached_target_readiness(monkeypatch):
    previous_cached = main.cached_devices
    previous_target = main.store.get_target_device()
    main.cached_devices = [{"id": "speaker-1", "name": "Speaker 1", "is_active": True, "supports_volume": True}]
    monkeypatch.setattr(main.store, "get_target_device", lambda: main.TargetDevice(device_id="speaker-1"))
    try:
        payload = main.mqtt_status_payload()
    finally:
        main.cached_devices = previous_cached
        monkeypatch.setattr(main.store, "get_target_device", lambda: previous_target)

    assert payload["target_readiness"]["safe_for_live_control"] is True
    assert payload["target_readiness"]["ready_for_live_control"] is True
    assert payload["target_readiness"]["active"] is True
    assert payload["target_readiness"]["volume_control_supported"] is True
    assert payload["target_readiness"]["last_update_at"] == payload["target_readiness"]["checked_at"]
    assert payload["target_readiness"]["resolved_device_id"] == "speaker-1"
    assert payload["target_readiness"]["risks"] == []


def test_target_device_readiness_reports_zero_volume_risk():
    readiness = main.target_device_readiness_from_devices(
        main.TargetDevice(device_id="speaker-1"),
        [{"id": "speaker-1", "name": "Speaker 1", "is_active": True, "supports_volume": True, "volume_percent": 0}],
        checked_at="2026-07-14T00:00:00+00:00",
    )

    assert readiness["safe_for_live_control"] is True
    assert readiness["ready_for_live_control"] is False
    assert readiness["muted_or_zero_volume"] is True
    assert readiness["risks"] == ["zero_volume"]


@pytest.mark.asyncio
async def test_verify_target_for_live_control_accepts_ready_target():
    client = FakeCommandSpotifyClient()
    target = main.TargetDevice(device_id="speaker-1")

    class Store:
        def get_target_device(self):
            return target

    response = await main.verify_target_for_live_control(client=client, state_store=Store())

    assert response["ok"] is True
    assert response["resolved_device_id"] == "speaker-1"
    assert response["readiness"]["ready_for_live_control"] is True
    assert response["readiness"]["risks"] == []


@pytest.mark.asyncio
async def test_verify_target_for_live_control_refuses_unready_target():
    client = FakeCommandSpotifyClient()
    target = main.TargetDevice(device_id="speaker-2")

    class Store:
        def get_target_device(self):
            return target

    with pytest.raises(main.HTTPException) as exc:
        await main.verify_target_for_live_control(client=client, state_store=Store())

    assert exc.value.status_code == 409
    assert exc.value.detail["readiness"]["safe_for_live_control"] is True
    assert exc.value.detail["readiness"]["ready_for_live_control"] is False
    assert exc.value.detail["readiness"]["risks"] == ["inactive_device", "volume_unavailable"]


@pytest.mark.asyncio
async def test_verify_target_for_live_control_requires_configured_target():
    client = FakeCommandSpotifyClient()

    class Store:
        def get_target_device(self):
            return None

    with pytest.raises(main.HTTPException) as exc:
        await main.verify_target_for_live_control(client=client, state_store=Store())

    assert exc.value.status_code == 409
    assert exc.value.detail["readiness"]["risks"] == ["target_not_configured"]


@pytest.mark.asyncio
async def test_rest_play_refuses_configured_target_that_is_not_ready(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []

    async def fake_publish_mqtt_status(
        command_type=None,
        command_request_id=None,
        command_pending=None,
        command_ok=None,
        command_error=None,
        force_publish=False,
    ):
        status_pulses.append((command_type, command_ok, command_error, force_publish))

    monkeypatch.setattr(main.store, "get_target_device", lambda: main.TargetDevice(device_id="speaker-2"))
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    with pytest.raises(main.HTTPException) as exc:
        await main.play(PlaybackCommand(), client=client)

    assert exc.value.status_code == 409
    assert exc.value.detail["readiness"]["ready_for_live_control"] is False
    assert exc.value.detail["readiness"]["risks"] == ["inactive_device", "volume_unavailable"]
    assert client.calls == []
    assert status_pulses == [
        ("play", False, "play target is not ready for live control: inactive_device,volume_unavailable", True)
    ]


@pytest.mark.asyncio
async def test_rest_seek_refuses_configured_target_that_is_not_ready(monkeypatch):
    client = FakeCommandSpotifyClient()

    monkeypatch.setattr(main.store, "get_target_device", lambda: main.TargetDevice(device_id="speaker-2"))

    with pytest.raises(main.HTTPException) as exc:
        await main.seek(SeekCommand(position_ms=42000), client=client)

    assert exc.value.status_code == 409
    assert exc.value.detail["readiness"]["ready_for_live_control"] is False
    assert exc.value.detail["readiness"]["risks"] == ["inactive_device", "volume_unavailable"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_rest_transfer_refuses_unsafe_target(monkeypatch):
    client = FakeCommandSpotifyClient()
    client.device_items = [{"id": "restricted-1", "name": "Restricted", "is_restricted": True}]
    monkeypatch.setattr(main.store, "set_target_device", lambda _target: None)

    with pytest.raises(main.HTTPException) as exc:
        await main.transfer_playback(TransferPlaybackCommand(device_id="restricted-1"), client=client)

    assert exc.value.status_code == 409
    assert exc.value.detail["readiness"]["risks"] == ["restricted_device", "inactive_device", "volume_unavailable"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_mqtt_transfer_refuses_unsafe_target(monkeypatch):
    client = FakeCommandSpotifyClient()
    client.device_items = []
    status_pulses = []

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok, command_error))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    with pytest.raises(ValueError, match="not safe for live control"):
        await main.handle_mqtt_command({"request_id": "knob-transfer-fail", "type": "transfer", "device_id": "missing"})

    assert client.calls == []
    assert status_pulses == [
        ("transfer", "knob-transfer-fail", True, None, None),
        (
            "transfer",
            "knob-transfer-fail",
            False,
            False,
            "transfer target is not safe for live control: target_not_found,missing_device_id",
        ),
    ]


@pytest.mark.asyncio
async def test_mqtt_next_refuses_configured_target_that_is_not_ready(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok, command_error))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main.store, "get_target_device", lambda: main.TargetDevice(device_id="speaker-2"))
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    with pytest.raises(main.TargetNotReadyForLiveControl):
        await main.handle_mqtt_command({"request_id": "knob-next-unready", "type": "next"})

    assert client.calls == []
    assert status_pulses == [
        ("next", "knob-next-unready", True, None, None),
        (
            "next",
            "knob-next-unready",
            False,
            False,
            "next target is not ready for live control: inactive_device,volume_unavailable",
        ),
    ]


@pytest.mark.asyncio
async def test_mqtt_seek_refuses_configured_target_that_is_not_ready(monkeypatch):
    client = FakeCommandSpotifyClient()
    status_pulses = []

    async def fake_publish_mqtt_status(command_type=None, command_request_id=None, command_pending=None, command_ok=None, command_error=None):
        status_pulses.append((command_type, command_request_id, command_pending, command_ok, command_error))

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main.store, "get_target_device", lambda: main.TargetDevice(device_id="speaker-2"))
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    with pytest.raises(main.TargetNotReadyForLiveControl):
        await main.handle_mqtt_command({"request_id": "knob-seek-unready", "type": "seek", "position_ms": 42000})

    assert client.calls == []
    assert status_pulses == [
        ("seek", "knob-seek-unready", True, None, None),
        (
            "seek",
            "knob-seek-unready",
            False,
            False,
            "seek target is not ready for live control: inactive_device,volume_unavailable",
        ),
    ]


@pytest.mark.asyncio
async def test_rest_target_changes_publish_status_pulse(monkeypatch):
    status_pulses = []
    targets = []

    async def fake_publish_mqtt_status(
        command_type=None,
        command_request_id=None,
        command_pending=None,
        command_ok=None,
        command_error=None,
        force_publish=False,
    ):
        _ = command_pending
        status_pulses.append((command_type, command_request_id, command_ok, force_publish))

    monkeypatch.setattr(main.store, "set_target_device", lambda target: targets.append(target))
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    client = FakeCommandSpotifyClient()
    monkeypatch.setattr(client, "spotify_configured", False, raising=False)

    await main.set_target(TargetDeviceCommand(device_id="speaker-1"), client=client, state_store=main.store)
    await main.set_target(TargetDeviceCommand(), client=client, state_store=main.store)

    assert [target.device_id if target else None for target in targets] == ["speaker-1", None]
    assert status_pulses == [("set_target", None, True, True), ("clear_target", None, True, True)]
