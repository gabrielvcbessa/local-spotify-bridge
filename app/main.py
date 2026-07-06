from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .broker import ConnectionBroker, StatePoller
from .config import get_settings
from .models import PlaybackCommand, SeekCommand, TransferPlaybackCommand, VolumeCommand
from .spotify import SpotifyAuthNotConfigured, SpotifyClient, SpotifyNotConfigured


settings = get_settings()
spotify = SpotifyClient(settings)
broker = ConnectionBroker(settings)
poller = StatePoller(spotify.current_playback, broker, settings.poll_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await broker.start()
    if settings.spotify_configured:
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
    return {
        "ok": True,
        "spotify_configured": settings.spotify_configured,
        "spotify_auth_configured": settings.spotify_auth_configured,
        "mqtt_enabled": settings.mqtt_enabled,
        "state_version": broker.version,
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
    except Exception as exc:
        raise translate_spotify_error(exc) from exc

    return {
        "message": "Copy refresh_token into SPOTIFY_REFRESH_TOKEN in your .env, then restart the bridge.",
        "refresh_token": token.get("refresh_token"),
        "access_token_expires_in": token.get("expires_in"),
        "scope": token.get("scope"),
        "token_type": token.get("token_type"),
    }


@app.get("/v1/state")
async def get_state(
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
        "state": state_broker.current_state.model_dump(mode="json")
        if state_broker.current_state
        else None,
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
        await client.play(body=body, device_id=command.device_id)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/pause", status_code=204)
async def pause(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.pause(device_id=device_id)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/next", status_code=204)
async def next_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.next_track(device_id=device_id)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/previous", status_code=204)
async def previous_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.previous_track(device_id=device_id)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/transfer", status_code=204)
async def transfer_playback(
    command: TransferPlaybackCommand,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.transfer_playback(command.device_id, command.play)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/seek", status_code=204)
async def seek(command: SeekCommand, client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify):
    try:
        await client.seek(command.position_ms, command.device_id)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/volume", status_code=204)
async def set_volume(
    command: VolumeCommand,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.set_volume(command.volume_percent, command.device_id)
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
