from contextlib import asynccontextmanager
from io import BytesIO
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from PIL import Image

from .art import ArtCache, ArtOptions, art_version, bytes_hash, display_ready_rgb565, image_to_rgb565
from .broker import ConnectionBroker, StatePoller
from .config import get_settings
from .knob import knob_snapshot
from .models import PlaybackCommand, SeekCommand, TargetDeviceCommand, TransferPlaybackCommand, VolumeCommand
from .spotify import (
    SpotifyAuthNotConfigured,
    SpotifyClient,
    SpotifyNotConfigured,
    compact_playlists,
    compact_tracks,
)
from .store import RuntimeStore, TargetDevice


settings = get_settings()
store = RuntimeStore(settings)
spotify = SpotifyClient(settings, store)
broker = ConnectionBroker(settings)
poller = StatePoller(spotify.current_playback, broker, settings.poll_interval_seconds)
art_cache = ArtCache(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await broker.start()
    if spotify.spotify_configured:
        poller.start()
    yield
    await poller.stop()
    await broker.stop()
    await spotify.close()


app = FastAPI(
    title="Local Spotify Bridge",
    description="REST, WebSocket, and MQTT bridge for local Spotify controls and playback state.",
    version="0.1.0",
    lifespan=lifespan,
)


def spotify_client() -> SpotifyClient:
    return spotify


def bridge_broker() -> ConnectionBroker:
    return broker


def runtime_store() -> RuntimeStore:
    return store


def translate_spotify_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SpotifyNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, SpotifyAuthNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    status_code = getattr(getattr(exc, "response", None), "status_code", 502)
    detail: Any = str(exc)
    try:
        detail = exc.response.json()
    except Exception:
        pass
    return HTTPException(status_code=status_code, detail=detail)


@app.exception_handler(SpotifyNotConfigured)
async def spotify_not_configured_handler(_, exc: SpotifyNotConfigured):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(SpotifyAuthNotConfigured)
async def spotify_auth_not_configured_handler(_, exc: SpotifyAuthNotConfigured):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, Any]:
    target = store.get_target_device()
    return {
        "ok": True,
        "spotify_configured": spotify.spotify_configured,
        "spotify_auth_configured": settings.spotify_auth_configured,
        "mqtt_enabled": settings.mqtt_enabled,
        "state_version": broker.version,
        "last_spotify_error": broker.last_spotify_error,
        "last_poll_at": broker.last_poll_at,
        "active_device_name": broker.current_state.device_name if broker.current_state else None,
        "target_device_name": target.device_name if target else None,
        "target_device_id": target.device_id if target else None,
    }


@app.get("/v1/auth/login")
async def auth_login(
    state: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    try:
        return {
            "authorize_url": client.authorize_url(state),
            "redirect_uri": settings.spotify_redirect_uri,
            "scopes": settings.spotify_scope_list,
        }
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/auth/callback")
async def auth_callback(
    code: str | None = None,
    error: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    if error:
        raise HTTPException(status_code=400, detail={"spotify_error": error})
    if not code:
        raise HTTPException(status_code=400, detail="Missing Spotify authorization code.")

    try:
        token = await client.exchange_code(code)
        if token.get("refresh_token"):
            poller.start()
    except Exception as exc:
        raise translate_spotify_error(exc) from exc

    return {
        "message": "Refresh token saved. The bridge is configured now; no restart is required.",
        "refresh_token": token.get("refresh_token"),
        "access_token_expires_in": token.get("expires_in"),
        "scope": token.get("scope"),
        "token_type": token.get("token_type"),
    }


@app.get("/v1/state")
async def get_state(
    request: Request,
    refresh: bool = False,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
    state_broker: Annotated[ConnectionBroker, Depends(bridge_broker)] = broker,
) -> dict[str, Any]:
    if refresh:
        try:
            await state_broker.publish_if_changed(await client.current_playback())
        except Exception as exc:
            raise translate_spotify_error(exc) from exc
    return {
        "version": state_broker.version,
        "state": state_payload(state_broker.current_state, request) if state_broker.current_state else None,
    }


@app.get("/v1/target")
async def get_target(
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
    state_store: Annotated[RuntimeStore, Depends(runtime_store)] = store,
) -> dict[str, Any]:
    target = state_store.get_target_device()
    resolved_device_id = None
    if target and client.spotify_configured:
        try:
            resolved_device_id = await client.resolve_target_device_id(target=target)
        except Exception as exc:
            broker.mark_spotify_error(exc)
    return {
        "target": target.model_dump(mode="json") if target else None,
        "resolved_device_id": resolved_device_id,
    }


@app.get("/v1/knob/snapshot")
async def get_knob_snapshot(
    request: Request,
    refresh: bool = False,
    art_size: int = Query(default=180, ge=32, le=640),
    art_format: str = Query(default="rgb565", pattern="^rgb565$"),
    swap: str = Query(default="lvgl", pattern="^(lvgl|none)$"),
    art_variant: str = Query(default="player-bg", pattern="^player-bg$"),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
    state_broker: Annotated[ConnectionBroker, Depends(bridge_broker)] = broker,
) -> dict[str, Any]:
    _ = art_format
    if refresh:
        try:
            await state_broker.publish_if_changed(await client.current_playback())
        except Exception as exc:
            raise translate_spotify_error(exc) from exc
    art_options = ArtOptions(size=art_size, swap=swap, variant=art_variant)
    art_hash = None
    if state_broker.current_state and state_broker.current_state.album_art_url:
        image_id = state_broker.current_state.album_art_id or state_broker.current_state.knob_art_version
        if image_id:
            try:
                art_payload = await cached_rgb565_art(
                    client,
                    image_id,
                    state_broker.current_state.album_art_url,
                    art_options,
                )
                art_hash = bytes_hash(art_payload)
            except Exception as exc:
                raise translate_spotify_error(exc) from exc

    return knob_snapshot(
        version=state_broker.version,
        state=state_broker.current_state,
        base_url=public_base_url(request),
        spotify_configured=client.spotify_configured,
        art_options=art_options,
        art_hash=art_hash,
    )


@app.post("/v1/target")
async def set_target(
    command: TargetDeviceCommand,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
    state_store: Annotated[RuntimeStore, Depends(runtime_store)] = store,
) -> dict[str, Any]:
    if not command.device_id and not command.device_name:
        state_store.set_target_device(None)
        return {"target": None, "resolved_device_id": None}

    target = TargetDevice(device_id=command.device_id, device_name=command.device_name)
    resolved_device_id = command.device_id
    try:
        if client.spotify_configured:
            resolved_device_id = await client.resolve_target_device_id(target=target)
        if command.transfer_playback and not client.spotify_configured:
            raise SpotifyNotConfigured(
                "Spotify credentials are required to transfer playback while setting target."
            )
        if command.transfer_playback and resolved_device_id:
            await client.transfer_playback(resolved_device_id, command.play)
            await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc

    state_store.set_target_device(target)
    return {
        "target": target.model_dump(mode="json"),
        "resolved_device_id": resolved_device_id,
    }


@app.websocket("/v1/ws")
async def websocket_state(websocket: WebSocket) -> None:
    await broker.add_websocket(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await broker.remove_websocket(websocket)


@app.post("/v1/control/play", status_code=204)
async def play(command: PlaybackCommand, client: Annotated[SpotifyClient, Depends(spotify_client)]):
    body = command.model_dump(exclude_none=True, exclude={"device_id"})
    try:
        device_id = await command_device_id(client, command.device_id)
        await client.play(body=body, device_id=device_id)
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/pause", status_code=204)
async def pause(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.pause(device_id=await command_device_id(client, device_id))
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/next", status_code=204)
async def next_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.next_track(device_id=await command_device_id(client, device_id))
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/previous", status_code=204)
async def previous_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.previous_track(device_id=await command_device_id(client, device_id))
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/transfer", status_code=204)
async def transfer_playback(
    command: TransferPlaybackCommand,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.transfer_playback(command.device_id, command.play)
        store.set_target_device(TargetDevice(device_id=command.device_id))
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/seek", status_code=204)
async def seek(command: SeekCommand, client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify):
    try:
        await client.seek(command.position_ms, await command_device_id(client, command.device_id))
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/volume", status_code=204)
async def set_volume(
    command: VolumeCommand,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        device_id = await command_device_id(client, command.device_id)
        await client.set_volume(command.volume_percent, device_id)
        await refresh_and_publish(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/devices")
async def devices(client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify):
    try:
        return await client.devices()
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/playlists")
async def playlists(
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return await client.playlists(limit=limit, offset=offset)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/library/playlists")
async def compact_library_playlists(
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return compact_playlists(await client.playlists(limit=limit, offset=offset)).model_dump(mode="json")
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/playlists/{playlist_id}/tracks")
async def playlist_tracks(
    playlist_id: str,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return await client.playlist_tracks(playlist_id, limit=limit, offset=offset)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/library/playlists/{playlist_id}/tracks")
async def compact_playlist_tracks(
    playlist_id: str,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        payload = await client.playlist_tracks(playlist_id, limit=limit, offset=offset)
        return compact_tracks(payload).model_dump(mode="json")
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/saved-tracks")
async def saved_tracks(
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return await client.saved_tracks(limit=limit, offset=offset)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/library/saved-tracks")
async def compact_saved_tracks(
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return compact_tracks(await client.saved_tracks(limit=limit, offset=offset)).model_dump(mode="json")
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/art/current.jpg")
async def current_art_jpg(
    size: int = Query(default=180, ge=32, le=640),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    if not broker.current_state or not broker.current_state.album_art_url:
        raise HTTPException(status_code=404, detail="No current album art available.")
    return await resized_image_response(client, broker.current_state.album_art_url, size, "JPEG")


@app.get("/v1/art/current.rgb565")
async def current_art_rgb565(
    size: int = Query(default=180, ge=32, le=640),
    swap: str = Query(default="lvgl", pattern="^(lvgl|none)$"),
    variant: str = Query(default="player-bg", pattern="^player-bg$"),
    theme: str = Query(default="dark", pattern="^(dark|none)$"),
    darken: float = Query(default=0.52, ge=0.0, le=0.85),
    saturation: float = Query(default=0.9, ge=0.0, le=3.0),
    contrast: float = Query(default=1.08, ge=0.0, le=3.0),
    blur: float = Query(default=0.0, ge=0.0, le=6.0),
    circle: bool = False,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    if not broker.current_state or not broker.current_state.album_art_url:
        raise HTTPException(status_code=404, detail="No current album art available.")
    image_id = broker.current_state.album_art_id or broker.current_state.knob_art_version
    if not image_id:
        raise HTTPException(status_code=404, detail="Current album art has no cacheable image id.")
    options = ArtOptions(
        size=size,
        theme=theme,
        swap=swap,
        variant=variant,
        darken=darken,
        saturation=saturation,
        contrast=contrast,
        blur=blur,
        circle=circle,
    )
    payload = await cached_rgb565_art(client, image_id, broker.current_state.album_art_url, options)
    return rgb565_response(payload, options, image_id)


@app.get("/v1/knob/art/current.rgb565")
async def knob_current_art_rgb565(
    size: int = Query(default=180, ge=32, le=640),
    swap: str = Query(default="lvgl", pattern="^(lvgl|none)$"),
    variant: str = Query(default="player-bg", pattern="^player-bg$"),
    theme: str = Query(default="dark", pattern="^(dark|none)$"),
    darken: float = Query(default=0.52, ge=0.0, le=0.85),
    saturation: float = Query(default=0.9, ge=0.0, le=3.0),
    contrast: float = Query(default=1.08, ge=0.0, le=3.0),
    blur: float = Query(default=0.0, ge=0.0, le=6.0),
    circle: bool = False,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    if not broker.current_state or not broker.current_state.album_art_url:
        raise HTTPException(status_code=404, detail="No current album art available.")
    image_id = broker.current_state.album_art_id or broker.current_state.knob_art_version
    if not image_id:
        raise HTTPException(status_code=404, detail="Current album art has no cacheable image id.")
    options = ArtOptions(
        size=size,
        theme=theme,
        swap=swap,
        variant=variant,
        darken=darken,
        saturation=saturation,
        contrast=contrast,
        blur=blur,
        circle=circle,
    )
    payload = await cached_rgb565_art(client, image_id, broker.current_state.album_art_url, options)
    return rgb565_response(payload, options, image_id)


@app.get("/v1/art/proxy.jpg")
async def proxy_art_jpg(
    url: str,
    size: int = Query(default=180, ge=32, le=640),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    return await resized_image_response(client, url, size, "JPEG")


@app.get("/v1/art/{spotify_image_id}.rgb565")
async def spotify_art_rgb565(
    spotify_image_id: str,
    size: int = Query(default=180, ge=32, le=640),
    swap: str = Query(default="lvgl", pattern="^(lvgl|none)$"),
    variant: str = Query(default="player-bg", pattern="^player-bg$"),
    theme: str = Query(default="dark", pattern="^(dark|none)$"),
    darken: float = Query(default=0.52, ge=0.0, le=0.85),
    saturation: float = Query(default=0.9, ge=0.0, le=3.0),
    contrast: float = Query(default=1.08, ge=0.0, le=3.0),
    blur: float = Query(default=0.0, ge=0.0, le=6.0),
    circle: bool = False,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    url = f"https://i.scdn.co/image/{spotify_image_id}"
    options = ArtOptions(
        size=size,
        theme=theme,
        swap=swap,
        variant=variant,
        darken=darken,
        saturation=saturation,
        contrast=contrast,
        blur=blur,
        circle=circle,
    )
    image_bytes = await cached_rgb565_art(client, spotify_image_id, url, options)
    return rgb565_response(image_bytes, options, spotify_image_id)


async def command_device_id(client: SpotifyClient, explicit_device_id: str | None) -> str | None:
    return await client.resolve_target_device_id(explicit_device_id, store.get_target_device())


async def refresh_and_publish(client: SpotifyClient) -> None:
    await broker.publish_if_changed(await client.current_playback())


def state_payload(state, request: Request) -> dict[str, Any]:
    payload = state.model_dump(mode="json")
    if state.album_art_id:
        payload["knob_art_url"] = f"{public_base_url(request)}/v1/art/current.rgb565?size=180&swap=lvgl"
        payload["knob_art_version"] = state.album_art_id
    return payload


def public_base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


async def cached_rgb565_art(
    client: SpotifyClient,
    image_id: str,
    url: str,
    options: ArtOptions,
) -> bytes:
    cached = art_cache.get(image_id, options)
    if cached is not None:
        return cached
    try:
        raw = await client.fetch_image(url)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc
    payload = display_ready_rgb565(raw, options)
    art_cache.set(image_id, options, payload)
    return payload


def rgb565_response(payload: bytes, options: ArtOptions, image_id: str) -> Response:
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "X-Image-Width": str(options.size),
            "X-Image-Height": str(options.size),
            "X-Image-Format": "rgb565",
            "X-Image-Byte-Order": options.byte_order,
            "X-Image-Variant": options.variant,
            "X-Image-Version": art_version(image_id, options),
            "X-Image-Hash": bytes_hash(payload),
            "Cache-Control": "public, max-age=86400",
        },
    )


async def resized_image_response(
    client: SpotifyClient,
    url: str,
    size: int,
    output_format: str,
) -> Response:
    image_bytes = await resized_image_bytes(client, url, size, output_format)
    return Response(content=image_bytes, media_type="image/jpeg")


async def resized_image_bytes(
    client: SpotifyClient,
    url: str,
    size: int,
    output_format: str,
) -> bytes:
    try:
        raw = await client.fetch_image(url)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc

    with Image.open(BytesIO(raw)) as image:
        image = image.convert("RGB")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (size, size), (0, 0, 0))
        x = (size - image.width) // 2
        y = (size - image.height) // 2
        canvas.paste(image, (x, y))

        if output_format == "RGB565":
            return image_to_rgb565(canvas)

        output = BytesIO()
        canvas.save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue()
