import base64
import asyncio
import time
from time import perf_counter
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .models import CompactLibraryItem, CompactPage, PlaybackSnapshot
from .rate_limit import SpotifyRateLimiter
from .store import RuntimeStore, TargetDevice
from .telemetry import telemetry


class SpotifyNotConfigured(RuntimeError):
    pass


class SpotifyAuthNotConfigured(RuntimeError):
    pass


def response_preview(response: httpx.Response | None) -> str | None:
    if response is None or not response.content:
        return None
    try:
        return response.text
    except Exception:
        return f"<{len(response.content)} binary bytes>"


class SpotifyClient:
    def __init__(
        self,
        settings: Settings,
        store: RuntimeStore | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._http = http or httpx.AsyncClient(timeout=20)
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._rate_limiters: dict[str, SpotifyRateLimiter] = {}
        self._default_rate_limiter = SpotifyRateLimiter(
            window_seconds=settings.spotify_rate_limit_window_seconds,
            soft_requests_per_window=settings.spotify_rate_limit_soft_requests_per_window,
            soft_ratio=settings.spotify_rate_limit_soft_ratio,
            backoff_multiplier=settings.spotify_rate_limit_backoff_multiplier,
            max_poll_interval_seconds=settings.spotify_rate_limit_max_poll_interval_seconds,
            retry_after_padding_seconds=settings.spotify_rate_limit_retry_after_padding_seconds,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _token(self) -> str:
        refresh_token = self.refresh_token
        if not self._settings.spotify_auth_configured or not refresh_token:
            raise SpotifyNotConfigured(
                "Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, and SPOTIFY_REFRESH_TOKEN."
            )

        if self._access_token and time.time() < self._token_expires_at - 30:
            return self._access_token

        auth = base64.b64encode(
            f"{self._settings.spotify_client_id}:{self._settings.spotify_client_secret}".encode()
        ).decode()
        response = await self._http.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {auth}"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._access_token

    @property
    def refresh_token(self) -> str:
        if self._store is not None:
            stored = self._store.get_refresh_token()
            if stored:
                return stored
        return self._settings.spotify_refresh_token

    @property
    def spotify_configured(self) -> bool:
        return bool(self._settings.spotify_auth_configured and self.refresh_token)

    def next_poll_interval(self, base_interval_seconds: float, group: str = "playback") -> float:
        return self._rate_limiter_for(group).poll_interval(base_interval_seconds)

    def rate_limit_status(self, base_poll_interval_seconds: float, group: str = "playback") -> dict[str, object]:
        status = self._rate_limiter_for(group).status(base_poll_interval_seconds)
        status["group"] = group
        return status

    def rate_limit_statuses(self, base_poll_interval_seconds_by_group: dict[str, float]) -> dict[str, object]:
        groups = {
            group: self.rate_limit_status(base_interval, group)
            for group, base_interval in base_poll_interval_seconds_by_group.items()
        }
        playback = groups.get("playback", self.rate_limit_status(0, "playback")).copy()
        playback["groups"] = groups
        return playback

    def authorize_url(self, state: str | None = None) -> str:
        if not self._settings.spotify_auth_configured:
            raise SpotifyAuthNotConfigured("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")

        params = {
            "client_id": self._settings.spotify_client_id,
            "response_type": "code",
            "redirect_uri": self._settings.spotify_redirect_uri,
            "scope": " ".join(self._settings.spotify_scope_list),
            "show_dialog": "true",
        }
        if state:
            params["state"] = state
        return f"https://accounts.spotify.com/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        if not self._settings.spotify_auth_configured:
            raise SpotifyAuthNotConfigured("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")

        auth = base64.b64encode(
            f"{self._settings.spotify_client_id}:{self._settings.spotify_client_secret}".encode()
        ).decode()
        response = await self._http.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {auth}"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.spotify_redirect_uri,
            },
        )
        response.raise_for_status()
        payload = response.json()
        refresh_token = payload.get("refresh_token")
        if refresh_token and self._store is not None:
            self._store.set_refresh_token(refresh_token)
            self._access_token = payload.get("access_token")
            self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return payload

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> Any:
        expected = expected_statuses or {200}
        token = await self._token()
        group = self._rate_limit_group(method, path)
        limiter = self._rate_limiter_for(group)
        wait_seconds = limiter.wait_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        limiter.record_request()
        started_at = perf_counter()
        response: httpx.Response | None = None
        error: str | None = None
        try:
            response = await self._http.request(
                method,
                f"https://api.spotify.com/v1{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json,
            )
            limiter.record_response(
                response.status_code,
                response.headers.get("Retry-After"),
            )
            if response.status_code not in expected:
                if response.is_error:
                    response.raise_for_status()
                if method.upper() != "GET":
                    return None
            if response.status_code == 204 or not response.content or not response.content.strip():
                return None
            return response.json()
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            telemetry.record_spotify_api(
                method=method,
                path=path,
                status_code=response.status_code if response is not None else None,
                latency_ms=(perf_counter() - started_at) * 1000,
                wait_seconds=wait_seconds,
                retry_after=response.headers.get("Retry-After") if response is not None else None,
                error=error,
                detail=response_preview(response),
            )

    def _rate_limiter_for(self, group: str) -> SpotifyRateLimiter:
        if group not in self._rate_limiters:
            self._rate_limiters[group] = SpotifyRateLimiter(
                window_seconds=self._default_rate_limiter.window_seconds,
                soft_requests_per_window=self._default_rate_limiter.soft_requests_per_window,
                soft_ratio=self._default_rate_limiter.soft_ratio,
                backoff_multiplier=self._default_rate_limiter.backoff_multiplier,
                max_poll_interval_seconds=self._default_rate_limiter.max_poll_interval_seconds,
                retry_after_padding_seconds=self._default_rate_limiter.retry_after_padding_seconds,
            )
        return self._rate_limiters[group]

    @staticmethod
    def _rate_limit_group(method: str, path: str) -> str:
        normalized_method = method.upper()
        if path == "/me/player/devices":
            return "devices"
        if path == "/me/playlists" or path.startswith("/playlists/"):
            return "playlists"
        if path == "/me/tracks":
            return "library"
        if normalized_method != "GET":
            return "commands"
        if path == "/me/player" or path == "/me/player/queue":
            return "playback"
        return "other"

    async def current_playback(self) -> PlaybackSnapshot | None:
        payload = await self.request("GET", "/me/player", expected_statuses={200, 204})
        if payload is None:
            return None
        state = normalize_playback(payload)
        if self._settings.spotify_preload_next_enabled:
            state.next_track = await self.next_queue_track()
        return state

    async def queue(self) -> Any:
        return await self.request("GET", "/me/player/queue")

    async def next_queue_track(self) -> dict[str, Any] | None:
        try:
            payload = await self.queue()
        except httpx.HTTPStatusError:
            return None
        queue = payload.get("queue", []) if isinstance(payload, dict) else []
        for item in queue:
            preview = compact_track_preview(item)
            if preview is not None:
                return preview
        return None

    async def play(self, body: dict[str, Any] | None = None, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("PUT", "/me/player/play", params=params, json=body or {}, expected_statuses={204})

    async def pause(self, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("PUT", "/me/player/pause", params=params, expected_statuses={204})

    async def next_track(self, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("POST", "/me/player/next", params=params, expected_statuses={204})

    async def previous_track(self, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("POST", "/me/player/previous", params=params, expected_statuses={204})

    async def transfer_playback(self, device_id: str, play: bool = True) -> None:
        await self.request(
            "PUT",
            "/me/player",
            json={"device_ids": [device_id], "play": play},
            expected_statuses={204},
        )

    async def seek(self, position_ms: int, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"position_ms": position_ms}
        if device_id:
            params["device_id"] = device_id
        await self.request("PUT", "/me/player/seek", params=params, expected_statuses={204})

    async def set_volume(self, volume_percent: int, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"volume_percent": volume_percent}
        if device_id:
            params["device_id"] = device_id
        await self.request("PUT", "/me/player/volume", params=params, expected_statuses={204})

    async def set_shuffle(self, enabled: bool, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"state": str(enabled).lower()}
        if device_id:
            params["device_id"] = device_id
        await self.request("PUT", "/me/player/shuffle", params=params, expected_statuses={204})

    async def set_repeat(self, mode: str, device_id: str | None = None) -> None:
        if mode not in {"off", "context", "track"}:
            raise ValueError("repeat mode must be off, context, or track.")
        params: dict[str, Any] = {"state": mode}
        if device_id:
            params["device_id"] = device_id
        await self.request("PUT", "/me/player/repeat", params=params, expected_statuses={204})

    async def save_track(self, track_id: str) -> None:
        await self.request("PUT", "/me/tracks", params={"ids": track_id}, expected_statuses={200, 204})

    async def remove_saved_track(self, track_id: str) -> None:
        await self.request("DELETE", "/me/tracks", params={"ids": track_id}, expected_statuses={200, 204})

    async def devices(self) -> Any:
        return await self.request("GET", "/me/player/devices")

    async def resolve_target_device_id(
        self,
        explicit_device_id: str | None = None,
        target: TargetDevice | None = None,
    ) -> str | None:
        if explicit_device_id:
            return explicit_device_id
        if target is None:
            return None

        payload = await self.devices()
        devices = payload.get("devices", []) if isinstance(payload, dict) else []
        if target.device_id:
            for device in devices:
                if device.get("id") == target.device_id:
                    return target.device_id
        if target.device_name:
            target_name = target.device_name.casefold()
            for device in devices:
                if str(device.get("name", "")).casefold() == target_name:
                    return device.get("id")
        return None

    async def playlists(self, limit: int = 50, offset: int = 0) -> Any:
        return await self.request("GET", "/me/playlists", params={"limit": limit, "offset": offset})

    async def playlist_tracks(self, playlist_id: str, limit: int = 100, offset: int = 0) -> Any:
        return await self.request(
            "GET",
            f"/playlists/{playlist_id}/tracks",
            params={"limit": limit, "offset": offset},
        )

    async def playlist(self, playlist_id: str) -> Any:
        return await self.request(
            "GET",
            f"/playlists/{playlist_id}",
            params={"fields": "id,name,uri"},
        )

    async def playlist_name(self, playlist_id: str) -> str | None:
        try:
            payload = await self.playlist(playlist_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {403, 404}:
                raise
            return await self.playlist_name_from_fallbacks(playlist_id)
        name = payload.get("name") if isinstance(payload, dict) else None
        if isinstance(name, str) and name:
            return name
        return await self.playlist_name_from_fallbacks(playlist_id)

    async def playlist_name_from_fallbacks(self, playlist_id: str) -> str | None:
        name = await self.playlist_name_from_user_library(playlist_id)
        if name:
            return name
        return await self.playlist_name_from_oembed(playlist_id)

    async def playlist_name_from_user_library(self, playlist_id: str, max_pages: int = 20) -> str | None:
        offset = 0
        limit = 50
        for _ in range(max_pages):
            payload = await self.playlists(limit=limit, offset=offset)
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items:
                if not isinstance(item, dict) or item.get("id") != playlist_id:
                    continue
                name = item.get("name")
                return name if isinstance(name, str) and name else None

            if not isinstance(payload, dict) or not payload.get("next") or len(items) < limit:
                return None
            offset += limit
        return None

    async def playlist_name_from_oembed(self, playlist_id: str) -> str | None:
        try:
            response = await self._http.get(
                "https://open.spotify.com/oembed",
                params={"url": f"https://open.spotify.com/playlist/{playlist_id}"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        payload = response.json()
        title = payload.get("title") if isinstance(payload, dict) else None
        return title if isinstance(title, str) and title else None

    async def saved_tracks(self, limit: int = 50, offset: int = 0) -> Any:
        return await self.request("GET", "/me/tracks", params={"limit": limit, "offset": offset})

    async def fetch_image(self, url: str) -> bytes:
        response = await self._http.get(url)
        response.raise_for_status()
        return response.content


def compact_playlists(payload: dict[str, Any]) -> CompactPage:
    items = []
    for playlist in payload.get("items", []):
        images = playlist.get("images") or []
        owner = playlist.get("owner") or {}
        tracks = playlist.get("tracks") or {}
        items.append(
            CompactLibraryItem(
                id=playlist.get("id"),
                uri=playlist.get("uri"),
                title=playlist.get("name") or "Untitled playlist",
                subtitle=owner.get("display_name"),
                image_url=images[0]["url"] if images else None,
                track_count=tracks.get("total"),
                owner_name=owner.get("display_name"),
            )
        )
    return compact_page(payload, items)


def compact_tracks(payload: dict[str, Any]) -> CompactPage:
    items = []
    for entry in payload.get("items", []):
        track = entry.get("track") or entry
        album = track.get("album") or {}
        images = album.get("images") or []
        artists = [artist.get("name", "") for artist in track.get("artists", []) if artist.get("name")]
        items.append(
            CompactLibraryItem(
                id=track.get("id"),
                uri=track.get("uri"),
                title=track.get("name") or "Untitled track",
                subtitle=", ".join(artists) if artists else album.get("name"),
                image_url=images[0]["url"] if images else None,
                duration_ms=track.get("duration_ms"),
                explicit=track.get("explicit"),
                playable=track.get("is_playable"),
            )
        )
    return compact_page(payload, items)


def compact_track_preview(track: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(track, dict) or track.get("type") != "track":
        return None

    album = track.get("album") or {}
    images = album.get("images") or []
    album_art_url = images[0]["url"] if images else None
    artists = [artist.get("name", "") for artist in track.get("artists", []) if artist.get("name")]
    album_art_id = spotify_image_id(album_art_url)
    return {
        "id": track.get("id"),
        "uri": track.get("uri"),
        "title": track.get("name"),
        "artists": artists,
        "artist_text": ", ".join(artists),
        "album": album.get("name"),
        "album_art_url": album_art_url,
        "album_art_id": album_art_id,
        "duration_ms": track.get("duration_ms"),
    }


def compact_page(payload: dict[str, Any], items: list[CompactLibraryItem]) -> CompactPage:
    limit = int(payload.get("limit") or len(items))
    offset = int(payload.get("offset") or 0)
    total = payload.get("total")
    next_offset = offset + limit if payload.get("next") else None
    return CompactPage(items=items, limit=limit, offset=offset, total=total, next_offset=next_offset)


def normalize_playback(payload: dict[str, Any]) -> PlaybackSnapshot:
    item = payload.get("item") or {}
    album = item.get("album") or {}
    images = album.get("images") or []
    album_art_url = images[0]["url"] if images else None
    album_art_id = spotify_image_id(album_art_url)
    device = payload.get("device") or {}
    artists = [artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")]

    return PlaybackSnapshot(
        is_playing=bool(payload.get("is_playing")),
        progress_ms=payload.get("progress_ms"),
        item_id=item.get("id"),
        item_uri=item.get("uri"),
        item_type=item.get("type"),
        title=item.get("name"),
        artists=artists,
        album=album.get("name"),
        album_art_url=album_art_url,
        album_art_id=album_art_id,
        knob_art_version=album_art_id,
        duration_ms=item.get("duration_ms"),
        device_id=device.get("id"),
        device_name=device.get("name"),
        device_type=device.get("type"),
        device_is_active=device.get("is_active"),
        device_volume_percent=device.get("volume_percent"),
        volume_control_supported=bool(device.get("supports_volume")),
        shuffle_state=payload.get("shuffle_state"),
        repeat_state=payload.get("repeat_state"),
        raw=payload,
    )


def spotify_image_id(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").split("/")[-1] or None
