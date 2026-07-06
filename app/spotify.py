import base64
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .models import CompactLibraryItem, CompactPage, PlaybackSnapshot
from .store import RuntimeStore, TargetDevice


class SpotifyNotConfigured(RuntimeError):
    pass


class SpotifyAuthNotConfigured(RuntimeError):
    pass


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
        response = await self._http.request(
            method,
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json,
        )
        if response.status_code not in expected:
            response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def current_playback(self) -> PlaybackSnapshot | None:
        payload = await self.request("GET", "/me/player", expected_statuses={200, 204})
        if payload is None:
            return None
        return normalize_playback(payload)

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
        return target.device_id

    async def playlists(self, limit: int = 50, offset: int = 0) -> Any:
        return await self.request("GET", "/me/playlists", params={"limit": limit, "offset": offset})

    async def playlist_tracks(self, playlist_id: str, limit: int = 100, offset: int = 0) -> Any:
        return await self.request(
            "GET",
            f"/playlists/{playlist_id}/tracks",
            params={"limit": limit, "offset": offset},
        )

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
