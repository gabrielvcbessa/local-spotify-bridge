from app.broker import states_are_meaningfully_different
from app.models import PlaybackSnapshot


def snapshot(**overrides):
    data = {
        "is_playing": True,
        "progress_ms": 10_000,
        "item_id": "track-1",
        "item_uri": "spotify:track:track-1",
        "title": "Song",
        "device_id": "device-1",
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

