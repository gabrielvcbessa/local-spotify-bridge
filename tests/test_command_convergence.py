import pytest

import app.main as main
from app.models import PlaybackSnapshot


class SequencedPlaybackClient:
    def __init__(self, states: list[PlaybackSnapshot | None]) -> None:
        self.states = list(states)
        self.calls = 0

    async def current_playback(self) -> PlaybackSnapshot | None:
        index = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return self.states[index]


@pytest.mark.asyncio
async def test_next_confirmation_retries_until_track_identity_changes():
    before = PlaybackSnapshot(item_id="track-1", item_uri="spotify:track:track-1", is_playing=True)
    client = SequencedPlaybackClient(
        [
            before.model_copy(deep=True),
            PlaybackSnapshot(item_id="track-2", item_uri="spotify:track:track-2", is_playing=True),
        ]
    )
    previous_state = main.broker.current_state
    main.broker.current_state = before.model_copy(deep=True)
    try:
        result = await main.refresh_until_command_converged(
            client,
            command_type="next",
            command={"type": "next"},
            before=before,
            expected_playing=True,
            follow_up_delays=(0.0,),
        )
    finally:
        main.broker.current_state = previous_state

    assert client.calls == 2
    assert result["state_confirmed"] is True
    assert result["confirmation_reason"] == "track_changed"
    assert result["observed_track_id"] == "track-2"


@pytest.mark.asyncio
async def test_next_confirmation_fails_when_authoritative_track_never_changes():
    before = PlaybackSnapshot(item_id="track-1", item_uri="spotify:track:track-1", is_playing=True)
    client = SequencedPlaybackClient([before.model_copy(deep=True), before.model_copy(deep=True)])
    previous_state = main.broker.current_state
    main.broker.current_state = before.model_copy(deep=True)
    try:
        result = await main.refresh_until_command_converged(
            client,
            command_type="next",
            command={"type": "next"},
            before=before,
            expected_playing=True,
            follow_up_delays=(0.0,),
        )
    finally:
        main.broker.current_state = previous_state

    assert client.calls == 2
    assert result["state_confirmed"] is False
    assert result["confirmation_reason"] == "track_unchanged"


def test_command_convergence_covers_playback_device_and_control_fields():
    before = PlaybackSnapshot(item_id="track-1", progress_ms=42000, is_playing=True)
    after = PlaybackSnapshot(
        item_id="track-1",
        progress_ms=0,
        is_playing=False,
        device_id="speaker-2",
        device_volume_percent=36,
        shuffle_state=True,
        repeat_state="context",
        item_saved=True,
    )

    assert main.command_state_convergence("pause", {}, before, after, False)[0] is True
    assert main.command_state_convergence("previous", {}, before, after, True)[0] is True
    assert main.command_state_convergence("volume_set", {"volume_percent": 36}, before, after, None)[0] is True
    assert main.command_state_convergence("shuffle_set", {"enabled": True}, before, after, None)[0] is True
    assert main.command_state_convergence("repeat_set", {"mode": "context"}, before, after, None)[0] is True
    assert main.command_state_convergence("save_current_track", {}, before, after, None)[0] is True
