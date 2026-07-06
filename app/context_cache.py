import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .models import PlaybackSnapshot

PLAYLIST_NAME_TTL_SECONDS = 24 * 60 * 60
PLAYLIST_FAILURE_TTL_SECONDS = 5 * 60


@dataclass
class PlaylistNameCacheEntry:
    name: str | None
    fetched_at: float
    expires_at: float
    failed: bool = False


class PlaylistNameCache:
    def __init__(self) -> None:
        self._entries: dict[str, PlaylistNameCacheEntry] = {}
        self._in_flight: set[str] = set()

    def get(self, playlist_id: str) -> str | None:
        entry = self._entries.get(playlist_id)
        if entry is None or entry.expires_at <= time.time() or entry.failed:
            return None
        return entry.name

    def should_resolve(self, playlist_id: str) -> bool:
        entry = self._entries.get(playlist_id)
        if playlist_id in self._in_flight:
            return False
        if entry is None:
            return True
        return entry.expires_at <= time.time()

    async def resolve_once(
        self,
        playlist_id: str,
        resolver: Callable[[str], Awaitable[str | None]],
    ) -> str | None:
        cached = self.get(playlist_id)
        if cached is not None:
            return cached
        if not self.should_resolve(playlist_id):
            return None

        self._in_flight.add(playlist_id)
        now = time.time()
        try:
            name = await resolver(playlist_id)
        except Exception:
            self._entries[playlist_id] = PlaylistNameCacheEntry(
                name=None,
                fetched_at=now,
                expires_at=now + PLAYLIST_FAILURE_TTL_SECONDS,
                failed=True,
            )
            raise
        finally:
            self._in_flight.discard(playlist_id)

        if not name:
            self._entries[playlist_id] = PlaylistNameCacheEntry(
                name=None,
                fetched_at=now,
                expires_at=now + PLAYLIST_FAILURE_TTL_SECONDS,
                failed=True,
            )
            return None

        self._entries[playlist_id] = PlaylistNameCacheEntry(
            name=name,
            fetched_at=now,
            expires_at=now + PLAYLIST_NAME_TTL_SECONDS,
            failed=False,
        )
        return name


def playback_context_parts(state: PlaybackSnapshot) -> dict[str, str | None]:
    uri = context_uri(state)
    context_type = context_type_from_uri(uri)
    return {
        "type": context_type,
        "uri": uri,
        "id": context_id_from_uri(uri),
        "name": context_name(state),
    }


def context_uri(state: PlaybackSnapshot) -> str | None:
    context = state.raw.get("context") if isinstance(state.raw, dict) else None
    if isinstance(context, dict):
        uri = context.get("uri")
        return uri if isinstance(uri, str) else None
    return None


def context_name(state: PlaybackSnapshot) -> str | None:
    context = state.raw.get("context") if isinstance(state.raw, dict) else None
    if isinstance(context, dict):
        name = context.get("name")
        return name if isinstance(name, str) and name else None
    return None


def context_type_from_uri(uri: str | None) -> str | None:
    parts = spotify_uri_parts(uri)
    return parts[1] if len(parts) >= 2 else None


def context_id_from_uri(uri: str | None) -> str | None:
    parts = spotify_uri_parts(uri)
    return parts[2] if len(parts) >= 3 else None


def spotify_uri_parts(uri: str | None) -> list[str]:
    if not uri:
        return []
    parts = uri.split(":")
    if len(parts) != 3 or parts[0] != "spotify":
        return []
    if not parts[1] or not parts[2]:
        return []
    return parts


def schedule_playlist_resolve(
    cache: PlaylistNameCache,
    playlist_id: str,
    resolver: Callable[[str], Awaitable[str | None]],
    on_success: Callable[[], Awaitable[None]],
    on_error: Callable[[Exception], None],
) -> None:
    if not cache.should_resolve(playlist_id):
        return

    async def resolve_background() -> None:
        try:
            name = await cache.resolve_once(playlist_id, resolver)
            if name:
                await on_success()
        except Exception as exc:
            on_error(exc)

    asyncio.create_task(resolve_background())
