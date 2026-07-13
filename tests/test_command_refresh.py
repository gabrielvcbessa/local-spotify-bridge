import pytest

import app.main as main
from app.models import PlaybackCommand, PlaybackSnapshot, TargetDeviceCommand, TransferPlaybackCommand


class FakeSpotifyClient:
    def __init__(self) -> None:
        self.calls = 0

    async def current_playback(self) -> PlaybackSnapshot:
        self.calls += 1
        return PlaybackSnapshot(title=f"Song {self.calls}")


class FakeCommandSpotifyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def play(self, body=None, device_id: str | None = None) -> None:
        self.calls.append(("play", device_id))

    async def pause(self, device_id: str | None = None) -> None:
        self.calls.append(("pause", device_id))

    async def next_track(self, device_id: str | None = None) -> None:
        self.calls.append(("next", device_id))

    async def previous_track(self, device_id: str | None = None) -> None:
        self.calls.append(("previous", device_id))

    async def transfer_playback(self, device_id: str, play: bool = True) -> None:
        self.calls.append(("transfer", device_id))


class FakeDevicesClient:
    def __init__(self) -> None:
        self.devices_calls = 0
        self.playlists_calls = 0
        self.saved_tracks_calls = 0

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


@pytest.mark.asyncio
async def test_refresh_and_publish_schedules_follow_up_refreshes(monkeypatch):
    client = FakeSpotifyClient()
    published: list[str | None] = []
    scheduled = []

    async def fake_publish_if_changed(state):
        published.append(state.title if state else None)
        return True

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(main.broker, "publish_if_changed", fake_publish_if_changed)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    await main.refresh_and_publish(client, follow_up_delays=(0.5, 1.5))

    assert published == ["Song 1"]
    assert len(scheduled) == 2


@pytest.mark.asyncio
async def test_mqtt_knob_snapshot_publishes_control_state(monkeypatch):
    published = []

    async def fake_publish_mqtt_retained(topic, payload):
        published.append((topic, payload))

    async def fake_publish_mqtt_art_payloads(_client, _state, _options):
        return None

    async def fake_publish_mqtt_status():
        return None

    async def fake_prewarm_cached_track_art(*_args):
        return None

    async def fake_resolved_context_name(*_args, **_kwargs):
        return None

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
    )

    await main.mqtt_knob_snapshot(7, state)

    control = [payload for topic, payload in published if topic == "control_state"]
    assert len(control) == 1
    assert control[0]["version"] == 7
    assert control[0]["playing"] is True
    assert control[0]["track_id"] == "track-1"
    assert control[0]["device"]["id"] == "device-1"
    assert control[0]["device"]["volume_percent"] == 42


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
    assert payload["pages"][2]["total"] == 1


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

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status():
        return None

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)

    await main.handle_mqtt_command({"type": "next"})
    await main.handle_mqtt_command({"type": "previous"})
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


@pytest.mark.asyncio
async def test_mqtt_transfer_refreshes_devices_and_state(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    device_refreshes = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_refresh_devices_and_publish(_client):
        device_refreshes.append(True)
        return {"items": []}

    async def fake_publish_mqtt_status():
        return None

    monkeypatch.setattr(main, "spotify", client)
    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "refresh_devices_and_publish", fake_refresh_devices_and_publish)
    monkeypatch.setattr(main, "publish_mqtt_status", fake_publish_mqtt_status)
    monkeypatch.setattr(main.store, "set_target_device", lambda _target: None)

    await main.handle_mqtt_command({"type": "transfer", "device_id": "speaker-1", "set_target": True})

    assert client.calls == [("transfer", "speaker-1")]
    assert device_refreshes == [True]
    assert refreshes == [main.settings.command_followup_refresh_delays_for("transfer")]


@pytest.mark.asyncio
async def test_mqtt_play_pause_does_not_use_implicit_target_device(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_publish_mqtt_status():
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
    refreshes = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_command_device_id(_client, device_id):
        return device_id

    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(main, "command_device_id", fake_command_device_id)

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


@pytest.mark.asyncio
async def test_rest_transfer_paths_use_transfer_follow_up_profile(monkeypatch):
    client = FakeCommandSpotifyClient()
    refreshes = []
    targets = []

    async def fake_refresh_and_publish(_client, *, follow_up_delays=()):
        refreshes.append(tuple(follow_up_delays))

    async def fake_resolve_target_device_id(*_args, **_kwargs):
        return "speaker-1"

    monkeypatch.setattr(main, "refresh_and_publish", fake_refresh_and_publish)
    monkeypatch.setattr(client, "spotify_configured", True, raising=False)
    monkeypatch.setattr(client, "resolve_target_device_id", fake_resolve_target_device_id, raising=False)
    monkeypatch.setattr(main.store, "set_target_device", lambda target: targets.append(target))

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
