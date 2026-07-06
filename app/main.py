import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from PIL import Image

from .art import ArtCache, ArtOptions, art_version, bytes_hash, color_bar_test_pattern_rgb565, display_ready_rgb565, image_to_rgb565
from .broker import ConnectionBroker, StatePoller
from .config import get_settings
from .context_cache import PlaylistNameCache, playback_context_parts, schedule_playlist_resolve
from .knob import knob_snapshot
from .knob_mqtt import devices_payload, envelope, library_page_payload, library_root_payload, status_payload
from .models import PlaybackCommand, PlaybackSnapshot, SeekCommand, TargetDeviceCommand, TransferPlaybackCommand, VolumeCommand
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
poller = StatePoller(
    spotify.current_playback,
    broker,
    settings.poll_interval_seconds,
    interval_strategy=spotify.next_poll_interval,
)
art_cache = ArtCache(settings)
playlist_name_cache = PlaylistNameCache()


@asynccontextmanager
async def lifespan(_: FastAPI):
    broker.set_mqtt_snapshot_factory(mqtt_knob_snapshot)
    broker.set_mqtt_config_factory(mqtt_knob_config)
    broker.set_mqtt_command_handler(handle_mqtt_command)
    broker.set_mqtt_request_handler(handle_mqtt_request)
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
        "playlist_name_cache": playlist_name_cache.status(),
        "art_cache": art_cache.status(),
        "rate_limit": spotify.rate_limit_status(settings.poll_interval_seconds),
        "mqtt_topics": broker.mqtt_topics() if settings.mqtt_enabled else None,
        "mqtt_availability": broker.last_mqtt_availability if settings.mqtt_enabled else None,
        "mqtt_availability_at": broker.last_mqtt_availability_at if settings.mqtt_enabled else None,
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
    art_size: int = Query(default=360, ge=32, le=640),
    art_format: str = Query(default="rotary-lvgl", pattern="^(rotary-lvgl|rgb565)$"),
    swap: str = Query(default="lvgl", pattern="^(lvgl|none)$"),
    art_variant: str = Query(default="player-bg", pattern="^player-bg$"),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
    state_broker: Annotated[ConnectionBroker, Depends(bridge_broker)] = broker,
) -> dict[str, Any]:
    if art_format == "rotary-lvgl":
        swap = "lvgl"
    if refresh:
        try:
            await state_broker.publish_if_changed(await client.current_playback())
        except Exception as exc:
            raise translate_spotify_error(exc) from exc
    art_options = ArtOptions(size=art_size, swap=swap, variant=art_variant)
    context_name = await resolved_context_name(
        client,
        state_broker.current_state,
        resolve_inline=refresh,
    )
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

    await prewarm_cached_track_art(client, state_broker.current_state, art_options)
    return knob_snapshot(
        version=state_broker.version,
        state=state_broker.current_state,
        base_url=public_base_url(request),
        spotify_configured=client.spotify_configured,
        art_options=art_options,
        art_hash=art_hash,
        context_name=context_name,
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
            await refresh_and_publish(client, follow_up_delays=settings.command_followup_refresh_delays)
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
        await refresh_and_publish(client, follow_up_delays=settings.command_followup_refresh_delays)
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
        await client.next_track(device_id=explicit_device_id(device_id))
        broker.mark_forward_transition_expected()
        await refresh_and_publish(client, follow_up_delays=settings.command_followup_refresh_delays)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/previous", status_code=204)
async def previous_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.previous_track(device_id=explicit_device_id(device_id))
        await refresh_and_publish(client, follow_up_delays=settings.command_followup_refresh_delays)
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
        await refresh_and_publish(client, follow_up_delays=settings.command_followup_refresh_delays)
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


@app.get("/v1/knob/status")
async def knob_status() -> dict[str, Any]:
    return mqtt_status_payload()


@app.get("/v1/knob/library/root")
async def knob_library_root(
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    try:
        return await build_library_root_payload(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/knob/library/page")
async def knob_library_page(
    kind: str = Query(pattern="^(playlists|saved_tracks|playlist_tracks|devices)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=3, ge=1, le=10),
    page: int = Query(default=0, ge=0, le=2),
    parent_uri: str | None = None,
    request_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    try:
        return await build_library_page_payload(
            client,
            request_id=request_id,
            page=page,
            kind=kind,
            offset=offset,
            limit=limit,
            parent_uri=parent_uri,
        )
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/knob/devices")
async def knob_devices(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=3, ge=1, le=10),
    request_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    try:
        return await build_devices_payload(client, request_id=request_id, offset=offset, limit=limit)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/knob/request")
async def knob_request(command: dict[str, Any]) -> dict[str, Any]:
    try:
        return await handle_mqtt_request(command)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/knob/command")
async def knob_command(command: dict[str, Any]) -> dict[str, Any]:
    try:
        return await handle_mqtt_command(command)
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
    size: int = Query(default=360, ge=32, le=640),
    format: str = Query(default="rotary-lvgl", pattern="^rotary-lvgl$"),
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
    _ = format
    swap = "lvgl"
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


@app.get("/v1/knob/art/test-pattern.rgb565")
async def knob_test_pattern_rgb565(
    size: int = Query(default=360, ge=32, le=640),
    format: str = Query(default="rotary-lvgl", pattern="^rotary-lvgl$"),
):
    _ = format
    options = ArtOptions(size=size, swap="lvgl", variant="test-pattern", theme="none", darken=0.0, saturation=1.0, contrast=1.0)
    payload = color_bar_test_pattern_rgb565(size, swap="lvgl")
    return rgb565_response(payload, options, "test-pattern")


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


def explicit_device_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def refresh_and_publish(
    client: SpotifyClient,
    *,
    follow_up_delays: tuple[float, ...] = (),
) -> None:
    await broker.publish_if_changed(await client.current_playback())
    for delay in follow_up_delays:
        asyncio.create_task(delayed_refresh_and_publish(client, delay))


async def delayed_refresh_and_publish(client: SpotifyClient, delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await broker.publish_if_changed(await client.current_playback())
    except Exception as exc:
        broker.mark_spotify_error(exc)


async def mqtt_knob_snapshot(version: int, state) -> dict[str, Any]:
    art_options = mqtt_art_options()
    context_name = await resolved_context_name(spotify, state, resolve_inline=False)
    art_hash = None
    if state and state.album_art_url:
        image_id = state.album_art_id or state.knob_art_version
        if image_id:
            try:
                art_payload = await cached_rgb565_art(spotify, image_id, state.album_art_url, art_options)
                art_hash = bytes_hash(art_payload)
            except Exception as exc:
                broker.mark_spotify_error(exc)

    await prewarm_cached_track_art(spotify, state, art_options)
    await publish_mqtt_art_payloads(spotify, state, art_options)
    await publish_mqtt_status()
    snapshot = knob_snapshot(
        version=version,
        state=state,
        base_url=mqtt_base_url(),
        spotify_configured=spotify.spotify_configured,
        art_options=art_options,
        art_hash=art_hash,
        context_name=context_name,
    )
    annotate_mqtt_art(snapshot, state, art_options)
    return snapshot


def mqtt_knob_config() -> dict[str, Any]:
    art_options = mqtt_art_options()
    topics = broker.mqtt_topics()
    return {
        "schema_version": 2,
        "device_id": settings.mqtt_knob_device_id,
        "qos": settings.mqtt_qos,
        "retain": {
            "state": True,
            "config": True,
            "library_root": True,
            "library_page": True,
            "devices": True,
            "status": True,
            "command_result": False,
            "request_result": False,
        },
        "topics": topics,
        "http": {
            "base_url": mqtt_base_url(),
            "snapshot_url": f"{mqtt_base_url()}/v1/knob/snapshot",
            "art_url": (
                f"{mqtt_base_url()}/v1/knob/art/current.rgb565"
                f"?size={art_options.size}&format=rotary-lvgl&variant={art_options.variant}"
            ),
        },
        "art": {
            "size": art_options.size,
            "format": "rgb565",
            "swap": art_options.swap,
            "variant": art_options.variant,
            "byte_order": art_options.byte_order,
            "topics": {
                "current": topics["art_current"],
                "next": topics["art_next"],
                "previous": topics["art_previous"],
            },
        },
        "commands": [
            "play_pause",
            "play",
            "pause",
            "next",
            "previous",
            "volume_set",
            "seek",
            "select_source",
            "transfer",
            "shuffle_set",
            "repeat_set",
            "play_library_item",
        ],
        "requests": ["library_root", "library_page", "devices", "refresh"],
        "limits": {
            "knob_visible_rows": 3,
            "library_page_limit": 3,
            "max_title_chars": 64,
            "max_subtitle_chars": 64,
        },
    }


def mqtt_art_options() -> ArtOptions:
    return ArtOptions(
        size=settings.mqtt_knob_art_size,
        swap=settings.mqtt_knob_art_swap,
        variant=settings.mqtt_knob_art_variant,
    )


def mqtt_base_url() -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    host = "localhost" if settings.host in {"0.0.0.0", "::"} else settings.host
    return f"http://{host}:{settings.port}"


def mqtt_status_payload() -> dict[str, Any]:
    return status_payload(
        version=broker.version,
        spotify_configured=spotify.spotify_configured,
        last_poll_at=broker.last_poll_at,
        last_error=broker.last_spotify_error,
        current_state=broker.current_state,
        target=store.get_target_device(),
        mqtt_connected=settings.mqtt_enabled,
    )


async def publish_mqtt_status() -> None:
    await broker.publish_mqtt_retained("status", mqtt_status_payload())


async def build_library_root_payload(client: SpotifyClient) -> dict[str, Any]:
    playlists_payload = await client.playlists(limit=1, offset=0)
    saved_payload = await client.saved_tracks(limit=1, offset=0)
    devices_raw = await client.devices()
    devices = devices_raw.get("devices", []) if isinstance(devices_raw, dict) else []
    return library_root_payload(
        version=broker.version,
        playlist_total=playlists_payload.get("total") if isinstance(playlists_payload, dict) else None,
        saved_total=saved_payload.get("total") if isinstance(saved_payload, dict) else None,
        device_total=len(devices),
    )


async def build_library_page_payload(
    client: SpotifyClient,
    *,
    request_id: str | None,
    page: int,
    kind: str,
    offset: int,
    limit: int,
    parent_uri: str | None = None,
) -> dict[str, Any]:
    if kind == "playlists":
        compact = compact_playlists(await client.playlists(limit=limit, offset=offset))
        return library_page_payload(
            version=broker.version,
            request_id=request_id,
            page=page,
            kind=kind,
            title="Playlists",
            compact=compact,
        )

    if kind == "saved_tracks":
        compact = compact_tracks(await client.saved_tracks(limit=limit, offset=offset))
        return library_page_payload(
            version=broker.version,
            request_id=request_id,
            page=page,
            kind=kind,
            title="Saved",
            compact=compact,
        )

    if kind == "playlist_tracks":
        playlist_id = playlist_id_from_uri(parent_uri)
        if not playlist_id:
            raise ValueError("playlist_tracks requires parent_uri spotify:playlist:{id}.")
        playlist_name = await client.playlist_name(playlist_id)
        compact = compact_tracks(await client.playlist_tracks(playlist_id, limit=limit, offset=offset))
        parent = {"id": playlist_id, "uri": parent_uri, "title": playlist_name or "Playlist"}
        return library_page_payload(
            version=broker.version,
            request_id=request_id,
            page=page,
            kind=kind,
            title=playlist_name or "Playlist",
            compact=compact,
            parent=parent,
        )

    if kind == "devices":
        return await build_devices_page_payload(client, request_id=request_id, page=page, offset=offset, limit=limit)

    raise ValueError(f"Unsupported library page kind: {kind}")


async def build_devices_payload(
    client: SpotifyClient,
    *,
    request_id: str | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    payload = await client.devices()
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    active_device_id = None
    for device in devices:
        if device.get("is_active"):
            active_device_id = device.get("id")
            break
    return devices_payload(
        version=broker.version,
        request_id=request_id,
        devices=devices,
        active_device_id=active_device_id,
        target=store.get_target_device(),
        offset=offset,
        limit=limit,
    )


async def build_devices_page_payload(
    client: SpotifyClient,
    *,
    request_id: str | None,
    page: int,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    devices = await build_devices_payload(client, request_id=request_id, offset=offset, limit=limit)
    items = [
        {
            "slot": item["slot"],
            "id": item["id"],
            "uri": f"spotify:device:{item['id']}" if item.get("id") else None,
            "title": item.get("name") or "Spotify device",
            "subtitle": item.get("type"),
            "image_url": None,
            "duration_ms": None,
            "track_count": None,
            "playable": True,
            "expandable": False,
            "item_kind": "device",
        }
        for item in devices["items"]
    ]
    payload = {
        "request_id": request_id,
        "page": page,
        "kind": "devices",
        "parent": None,
        "title": "Devices",
        "offset": devices["offset"],
        "limit": devices["limit"],
        "total": devices["total"],
        "items": items,
    }
    return envelope(version=broker.version, payload=payload, hash_payload={k: v for k, v in payload.items() if k != "request_id"})


async def prewarm_cached_track_art(client: SpotifyClient, state: PlaybackSnapshot | None, art_options: ArtOptions) -> None:
    if state is None:
        return
    for cached_track in (state.next_track, state.previous_track):
        if isinstance(cached_track, dict):
            await prewarm_track_art(client, cached_track, art_options)


async def prewarm_track_art(client: SpotifyClient, track: dict[str, Any], art_options: ArtOptions) -> None:
    image_id = track.get("album_art_id")
    image_url = track.get("album_art_url")
    if not isinstance(image_id, str) or not image_id:
        return
    if not isinstance(image_url, str) or not image_url:
        return
    try:
        await cached_rgb565_art(client, image_id, image_url, art_options)
    except Exception as exc:
        broker.mark_spotify_error(exc)


async def publish_mqtt_art_payloads(client: SpotifyClient, state: PlaybackSnapshot | None, art_options: ArtOptions) -> None:
    if state is None:
        return
    current_image_id = state.album_art_id or state.knob_art_version
    if current_image_id and state.album_art_url:
        try:
            payload = await cached_rgb565_art(client, current_image_id, state.album_art_url, art_options)
            await broker.publish_mqtt_retained_bytes("art/current/rgb565", payload)
        except Exception as exc:
            broker.mark_spotify_error(exc)

    for topic_key, track in (
        ("art/next/rgb565", state.next_track),
        ("art/previous/rgb565", state.previous_track),
    ):
        if not isinstance(track, dict):
            continue
        image_id = track.get("album_art_id")
        image_url = track.get("album_art_url")
        if not isinstance(image_id, str) or not image_id:
            continue
        if not isinstance(image_url, str) or not image_url:
            continue
        try:
            payload = await cached_rgb565_art(client, image_id, image_url, art_options)
            await broker.publish_mqtt_retained_bytes(topic_key, payload)
        except Exception as exc:
            broker.mark_spotify_error(exc)


def annotate_mqtt_art(snapshot: dict[str, Any], state: PlaybackSnapshot | None, art_options: ArtOptions) -> None:
    if state is None:
        return

    current_image_id = state.album_art_id or state.knob_art_version
    if current_image_id and isinstance(snapshot.get("art"), dict):
        add_mqtt_art_fields(snapshot["art"], current_image_id, art_options, "art/current/rgb565")

    for key, topic_key in (
        ("next_track", "art/next/rgb565"),
        ("previous_track", "art/previous/rgb565"),
    ):
        track = snapshot.get(key)
        if not isinstance(track, dict):
            continue
        art = track.get("art")
        image_id = track.get("album_art_id")
        if isinstance(art, dict) and isinstance(image_id, str) and image_id:
            add_mqtt_art_fields(art, image_id, art_options, topic_key)


def add_mqtt_art_fields(art: dict[str, Any], image_id: str, art_options: ArtOptions, topic_key: str) -> None:
    art["mqtt_topic"] = broker.mqtt_topic(topic_key)
    art["local_cache_path"] = str(art_cache.path_for(image_id, art_options))


async def handle_mqtt_request(command: dict[str, Any]) -> dict[str, Any]:
    request_type = command.get("type")
    request_id = command.get("request_id") if isinstance(command.get("request_id"), str) else None
    offset = int(command.get("offset") or 0)
    limit = int(command.get("limit") or 3)
    page = int(command.get("page") or 0)

    if request_type == "library_root":
        payload = await build_library_root_payload(spotify)
        await broker.publish_mqtt_retained("library/root", payload)
        return {"published_topic": broker.mqtt_topic("library/root"), "published_version": payload["version"]}

    if request_type == "library_page":
        kind = command.get("kind")
        if not isinstance(kind, str):
            raise ValueError("library_page requires string kind.")
        payload = await build_library_page_payload(
            spotify,
            request_id=request_id,
            page=page,
            kind=kind,
            offset=offset,
            limit=limit,
            parent_uri=command.get("parent_uri") if isinstance(command.get("parent_uri"), str) else None,
        )
        await broker.publish_mqtt_retained("library/page", payload)
        return {"published_topic": broker.mqtt_topic("library/page"), "published_version": payload["version"]}

    if request_type == "devices":
        payload = await build_devices_payload(spotify, request_id=request_id, offset=offset, limit=limit)
        await broker.publish_mqtt_retained("devices", payload)
        page_payload = await build_devices_page_payload(spotify, request_id=request_id, page=2, offset=offset, limit=limit)
        await broker.publish_mqtt_retained("library/page", page_payload)
        return {"published_topic": broker.mqtt_topic("devices"), "published_version": payload["version"]}

    if request_type == "refresh":
        await refresh_and_publish(spotify)
        await publish_mqtt_status()
        return {"state_version": broker.version}

    raise ValueError(f"Unsupported MQTT request type: {request_type}")


async def handle_mqtt_command(command: dict[str, Any]) -> dict[str, Any]:
    command_type = command.get("type")
    if not isinstance(command_type, str):
        raise ValueError("MQTT command requires a string 'type'.")
    follow_up_refresh = False

    if command_type == "play_pause":
        if broker.current_state and broker.current_state.is_playing:
            await spotify.pause(device_id=explicit_device_id(command.get("device_id")))
        else:
            await spotify.play(device_id=explicit_device_id(command.get("device_id")))
            follow_up_refresh = True
    elif command_type == "play":
        body = playback_body_from_mqtt(command)
        await spotify.play(body=body, device_id=await command_device_id(spotify, command.get("device_id")))
        follow_up_refresh = True
    elif command_type == "pause":
        await spotify.pause(device_id=explicit_device_id(command.get("device_id")))
    elif command_type == "next":
        await spotify.next_track(device_id=explicit_device_id(command.get("device_id")))
        broker.mark_forward_transition_expected()
        follow_up_refresh = True
    elif command_type == "previous":
        await spotify.previous_track(device_id=explicit_device_id(command.get("device_id")))
        follow_up_refresh = True
    elif command_type == "volume_set":
        volume_percent = command.get("volume_percent")
        if not isinstance(volume_percent, int):
            raise ValueError("volume_set requires integer volume_percent.")
        if not 0 <= volume_percent <= 100:
            raise ValueError("volume_percent must be between 0 and 100.")
        if broker.current_state and not broker.current_state.volume_control_supported:
            return {"ignored": True, "reason": "volume_control_unsupported"}
        await spotify.set_volume(volume_percent, await command_device_id(spotify, command.get("device_id")))
    elif command_type == "seek":
        position_ms = command.get("position_ms")
        if not isinstance(position_ms, int) or position_ms < 0:
            raise ValueError("seek requires non-negative integer position_ms.")
        await spotify.seek(position_ms, await command_device_id(spotify, command.get("device_id")))
    elif command_type == "select_source":
        context_uri = command.get("uri") or command.get("context_uri")
        if not isinstance(context_uri, str) or not context_uri:
            raise ValueError("select_source requires uri or context_uri.")
        await spotify.play(
            body={"context_uri": context_uri},
            device_id=await command_device_id(spotify, command.get("device_id")),
        )
        follow_up_refresh = True
    elif command_type == "transfer":
        device_id = command.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("transfer requires device_id.")
        play = command.get("play", True)
        await spotify.transfer_playback(device_id, bool(play))
        if command.get("set_target"):
            store.set_target_device(TargetDevice(device_id=device_id))
        follow_up_refresh = True
    elif command_type == "shuffle_set":
        enabled = command.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("shuffle_set requires boolean enabled.")
        await spotify.set_shuffle(enabled, await command_device_id(spotify, command.get("device_id")))
    elif command_type == "repeat_set":
        mode = command.get("mode")
        if not isinstance(mode, str):
            raise ValueError("repeat_set requires string mode.")
        await spotify.set_repeat(mode, await command_device_id(spotify, command.get("device_id")))
    elif command_type == "play_library_item":
        body = play_library_item_body(command)
        await spotify.play(body=body, device_id=await command_device_id(spotify, command.get("device_id")))
        follow_up_refresh = True
    else:
        raise ValueError(f"Unsupported MQTT command type: {command_type}")

    await refresh_and_publish(
        spotify,
        follow_up_delays=settings.command_followup_refresh_delays if follow_up_refresh else (),
    )
    await publish_mqtt_status()
    return {"state_version": broker.version, "published_state": True}


def playback_body_from_mqtt(command: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for source, target in (
        ("context_uri", "context_uri"),
        ("uri", "context_uri"),
        ("uris", "uris"),
        ("offset", "offset"),
        ("position_ms", "position_ms"),
    ):
        if command.get(source) is not None:
            body[target] = command[source]
    return body


def play_library_item_body(command: dict[str, Any]) -> dict[str, Any]:
    context_uri = command.get("context_uri")
    item_uri = command.get("item_uri")
    source_kind = command.get("source_kind")
    if isinstance(context_uri, str) and context_uri:
        offset = command.get("offset")
        body: dict[str, Any] = {"context_uri": context_uri}
        if isinstance(offset, dict):
            body["offset"] = offset
        elif isinstance(item_uri, str) and item_uri:
            body["offset"] = {"uri": item_uri}
        if isinstance(command.get("position_ms"), int):
            body["position_ms"] = command["position_ms"]
        return body

    if source_kind == "saved_tracks":
        uris = command.get("uris")
        if isinstance(uris, list) and all(isinstance(uri, str) for uri in uris):
            body = {"uris": uris}
            if isinstance(item_uri, str) and item_uri in uris:
                body["offset"] = {"position": uris.index(item_uri)}
            return body
        if isinstance(item_uri, str) and item_uri:
            return {"uris": [item_uri]}

    raise ValueError("play_library_item requires context_uri or saved_tracks item_uri.")


def playlist_id_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    parts = uri.split(":")
    if len(parts) != 3 or parts[0] != "spotify" or parts[1] != "playlist" or not parts[2]:
        return None
    return parts[2]


async def resolved_context_name(
    client: SpotifyClient,
    state,
    *,
    resolve_inline: bool,
) -> str | None:
    if state is None:
        return None
    context = playback_context_parts(state)
    if context["type"] != "playlist" or not context["id"]:
        return state.album
    if context["name"]:
        return context["name"]

    playlist_id = context["id"]
    cached_name = playlist_name_cache.get(playlist_id)
    if cached_name is not None:
        return cached_name

    if not client.spotify_configured:
        return None

    if resolve_inline:
        try:
            name = await playlist_name_cache.resolve_once(playlist_id, client.playlist_name)
            if name:
                await broker.publish_metadata_changed()
            return name
        except Exception as exc:
            broker.mark_spotify_error(exc)
            return None

    schedule_playlist_resolve(
        playlist_name_cache,
        playlist_id,
        client.playlist_name,
        broker.publish_metadata_changed,
        broker.mark_spotify_error,
    )
    return None


def state_payload(state, request: Request) -> dict[str, Any]:
    payload = state.model_dump(mode="json")
    if state.album_art_id:
        payload["knob_art_url"] = (
            f"{public_base_url(request)}/v1/knob/art/current.rgb565"
            f"?size={settings.mqtt_knob_art_size}&format=rotary-lvgl&variant=player-bg"
        )
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
            "X-Image-Target": "rotary-os-lvgl-image-source" if options.byte_order == "rotary-lvgl" else "generic-rgb565",
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
