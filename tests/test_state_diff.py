import pytest

from app.broker import ConnectionBroker, states_are_meaningfully_different
from app.config import Settings
from app.models import PlaybackSnapshot


def snapshot(**overrides):
    data = {
        "is_playing": True,
        "progress_ms": 10_000,
        "item_id": "track-1",
        "item_uri": "spotify:track:track-1",
        "title": "Song",
        "artists": ["Artist"],
        "album": "Album",
        "device_id": "device-1",
        "raw": {
            "context": {"type": "playlist", "uri": "spotify:playlist:playlist-1"},
            "item": {"album": {"uri": "spotify:album:album-1"}},
        },
    }
    data.update(overrides)
    return PlaybackSnapshot(**data)


def test_track_change_is_meaningful():
    assert states_are_meaningfully_different(
        snapshot(),
        snapshot(item_id="track-2", item_uri="spotify:track:track-2"),
        progress_drift_ms=5000,
    )


def test_small_progress_drift_is_ignored():
    assert not states_are_meaningfully_different(
        snapshot(progress_ms=10_000),
        snapshot(progress_ms=13_000),
        progress_drift_ms=5000,
    )


def test_large_progress_drift_is_meaningful():
    assert states_are_meaningfully_different(
        snapshot(progress_ms=10_000),
        snapshot(progress_ms=17_000),
        progress_drift_ms=5000,
    )


def test_volume_capability_change_is_meaningful():
    assert states_are_meaningfully_different(
        snapshot(volume_control_supported=False),
        snapshot(volume_control_supported=True),
        progress_drift_ms=5000,
    )


def test_next_track_change_is_meaningful():
    assert states_are_meaningfully_different(
        snapshot(next_track={"id": "track-2", "uri": "spotify:track:track-2"}),
        snapshot(next_track={"id": "track-3", "uri": "spotify:track:track-3"}),
        progress_drift_ms=5000,
    )


@pytest.mark.anyio
async def test_broker_tracks_previous_after_explicit_next_in_same_context():
    broker = ConnectionBroker(Settings())

    await broker.publish_if_changed(
        snapshot(
            item_id="track-1",
            item_uri="spotify:track:track-1",
            title="First song",
            album_art_id="art-1",
            album_art_url="https://i.scdn.co/image/art-1",
            duration_ms=180_000,
            next_track={"id": "track-2", "uri": "spotify:track:track-2"},
        )
    )
    broker.mark_forward_transition_expected()
    await broker.publish_if_changed(
        snapshot(
            item_id="track-2",
            item_uri="spotify:track:track-2",
            title="Second song",
            album_art_id="art-2",
            album_art_url="https://i.scdn.co/image/art-2",
            duration_ms=181_000,
        )
    )

    assert broker.current_state is not None
    assert broker.current_state.previous_track is not None
    assert broker.current_state.previous_track["id"] == "track-1"
    assert broker.current_state.previous_track["title"] == "First song"
    assert broker.current_state.previous_track["context_uri"] == "spotify:playlist:playlist-1"


@pytest.mark.anyio
async def test_broker_tracks_previous_when_song_finishes_in_same_context():
    broker = ConnectionBroker(Settings())

    await broker.publish_if_changed(
        snapshot(
            item_id="track-1",
            item_uri="spotify:track:track-1",
            progress_ms=176_000,
            duration_ms=180_000,
            title="Finishing song",
            album_art_id="art-1",
            album_art_url="https://i.scdn.co/image/art-1",
            next_track={"id": "track-2", "uri": "spotify:track:track-2"},
        )
    )
    await broker.publish_if_changed(
        snapshot(
            item_id="track-2",
            item_uri="spotify:track:track-2",
            progress_ms=0,
            duration_ms=181_000,
            title="Next song",
            album_art_id="art-2",
            album_art_url="https://i.scdn.co/image/art-2",
        )
    )

    assert broker.current_state is not None
    assert broker.current_state.previous_track is not None
    assert broker.current_state.previous_track["id"] == "track-1"


@pytest.mark.anyio
async def test_broker_drops_previous_on_source_change_even_after_next():
    broker = ConnectionBroker(Settings())

    await broker.publish_if_changed(
        snapshot(
            item_id="track-1",
            item_uri="spotify:track:track-1",
            title="Playlist song",
            album_art_id="art-1",
            album_art_url="https://i.scdn.co/image/art-1",
            next_track={"id": "track-2", "uri": "spotify:track:track-2"},
        )
    )
    broker.mark_forward_transition_expected()
    await broker.publish_if_changed(
        snapshot(
            item_id="track-2",
            item_uri="spotify:track:track-2",
            title="Other playlist song",
            album_art_id="art-2",
            album_art_url="https://i.scdn.co/image/art-2",
            raw={
                "context": {"type": "playlist", "uri": "spotify:playlist:playlist-2"},
                "item": {"album": {"uri": "spotify:album:album-2"}},
            },
        )
    )

    assert broker.current_state is not None
    assert broker.current_state.previous_track is None


@pytest.mark.anyio
async def test_broker_drops_previous_when_track_changes_without_forward_or_finish():
    broker = ConnectionBroker(Settings())

    await broker.publish_if_changed(
        snapshot(
            item_id="track-1",
            item_uri="spotify:track:track-1",
            progress_ms=30_000,
            duration_ms=180_000,
            title="Interrupted song",
            album_art_id="art-1",
            album_art_url="https://i.scdn.co/image/art-1",
        )
    )
    await broker.publish_if_changed(
        snapshot(
            item_id="track-2",
            item_uri="spotify:track:track-2",
            progress_ms=0,
            duration_ms=181_000,
            title="Different song",
            album_art_id="art-2",
            album_art_url="https://i.scdn.co/image/art-2",
        )
    )

    assert broker.current_state is not None
    assert broker.current_state.previous_track is None


@pytest.mark.anyio
async def test_broker_drops_previous_when_new_track_was_not_previous_next_track():
    broker = ConnectionBroker(Settings())

    await broker.publish_if_changed(
        snapshot(
            item_id="track-1",
            item_uri="spotify:track:track-1",
            title="First song",
            album_art_id="art-1",
            album_art_url="https://i.scdn.co/image/art-1",
            duration_ms=180_000,
            next_track={"id": "track-expected", "uri": "spotify:track:track-expected"},
        )
    )
    broker.mark_forward_transition_expected()
    await broker.publish_if_changed(
        snapshot(
            item_id="track-2",
            item_uri="spotify:track:track-2",
            title="Unexpected song",
            album_art_id="art-2",
            album_art_url="https://i.scdn.co/image/art-2",
            duration_ms=181_000,
        )
    )

    assert broker.current_state is not None
    assert broker.current_state.previous_track is None
