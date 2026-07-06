from typing import Any

from pydantic import BaseModel, Field


class PlaybackSnapshot(BaseModel):
    source: str = "spotify"
    is_playing: bool = False
    progress_ms: int | None = None
    item_id: str | None = None
    item_uri: str | None = None
    item_type: str | None = None
    title: str | None = None
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    album_art_url: str | None = None
    album_art_id: str | None = None
    knob_art_url: str | None = None
    knob_art_version: str | None = None
    duration_ms: int | None = None
    device_id: str | None = None
    device_name: str | None = None
    device_type: str | None = None
    device_is_active: bool | None = None
    device_volume_percent: int | None = None
    volume_control_supported: bool = False
    shuffle_state: bool | None = None
    repeat_state: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class StateEnvelope(BaseModel):
    event: str
    state: PlaybackSnapshot | None = None
    version: int


class PlaybackCommand(BaseModel):
    device_id: str | None = None
    context_uri: str | None = None
    uris: list[str] | None = None
    offset: dict[str, Any] | None = None
    position_ms: int | None = None


class TransferPlaybackCommand(BaseModel):
    device_id: str
    play: bool = True


class SeekCommand(BaseModel):
    position_ms: int
    device_id: str | None = None


class VolumeCommand(BaseModel):
    volume_percent: int = Field(ge=0, le=100)
    device_id: str | None = None


class TargetDeviceCommand(BaseModel):
    device_id: str | None = None
    device_name: str | None = None
    transfer_playback: bool = False
    play: bool = True


class CompactLibraryItem(BaseModel):
    id: str | None = None
    uri: str | None = None
    title: str
    subtitle: str | None = None
    image_url: str | None = None
    duration_ms: int | None = None
    track_count: int | None = None
    owner_name: str | None = None
    explicit: bool | None = None
    playable: bool | None = None


class CompactPage(BaseModel):
    items: list[CompactLibraryItem]
    limit: int
    offset: int
    total: int | None = None
    next_offset: int | None = None
