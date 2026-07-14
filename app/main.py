import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image

from .art import ArtCache, ArtOptions, art_version, bytes_hash, color_bar_test_pattern_rgb565, display_ready_rgb565, image_to_rgb565
from .broker import ConnectionBroker, PeriodicPoller, StatePoller
from .config import get_settings
from .context_cache import PlaylistNameCache, playback_context_parts, schedule_playlist_resolve
from .knob import knob_snapshot
from .knob_mqtt import devices_payload, envelope, library_item_payload, library_page_payload, library_root_payload, status_payload
from .models import PlaybackCommand, PlaybackSnapshot, SeekCommand, TargetDeviceCommand, TransferPlaybackCommand, VolumeCommand
from .mqtt_commands import mqtt_command_policy, play_library_item_body, playback_body_from_mqtt, playlist_id_from_uri
from .mqtt_contract import MQTT_KNOB_BACKEND_CAPABILITIES, mqtt_control_state_payload, mqtt_knob_config_payload, mqtt_protocol_payload
from .spotify import (
    SpotifyAuthNotConfigured,
    SpotifyClient,
    SpotifyNotConfigured,
    compact_recent_tracks,
    compact_playlists,
    compact_tracks,
)
from .store import RuntimeStore, TargetDevice
from .telemetry import DEFAULT_PERIODS_SECONDS, telemetry


settings = get_settings()
telemetry.configure(
    max_events=settings.debug_telemetry_max_events,
    retention_seconds=settings.debug_telemetry_retention_seconds,
)
store = RuntimeStore(settings)
spotify = SpotifyClient(settings, store)
broker = ConnectionBroker(settings)


def has_active_consumers() -> bool:
    return broker.has_active_consumers(ttl_seconds=settings.active_consumer_ttl_seconds)


def consumer_idle_explanation(consumer_status: dict[str, Any]) -> dict[str, Any]:
    websocket_count = int(consumer_status.get("websocket_count") or 0)
    ttl_seconds = float(consumer_status.get("ttl_seconds") or settings.active_consumer_ttl_seconds)
    last_activity_at = consumer_status.get("mqtt_last_activity_at")
    last_activity = consumer_status.get("mqtt_last_activity")
    mqtt_age_seconds = None
    if isinstance(last_activity_at, str):
        try:
            mqtt_age_seconds = (datetime.now(UTC) - datetime.fromisoformat(last_activity_at)).total_seconds()
        except ValueError:
            mqtt_age_seconds = None

    offline = isinstance(last_activity, dict) and last_activity.get("source") == "availability" and last_activity.get("online") is False
    if websocket_count > 0:
        reason = "websocket_connected"
    elif offline:
        reason = "mqtt_availability_offline"
    elif consumer_status.get("mqtt_active"):
        reason = "recent_mqtt_activity"
    elif last_activity_at is None:
        reason = "no_websocket_or_mqtt_activity"
    elif mqtt_age_seconds is None:
        reason = "invalid_mqtt_activity_timestamp"
    else:
        reason = "mqtt_activity_expired"

    return {
        "active": bool(consumer_status.get("active")),
        "reason": reason,
        "websocket_count": websocket_count,
        "mqtt_active": bool(consumer_status.get("mqtt_active")),
        "mqtt_last_activity_at": last_activity_at,
        "mqtt_last_activity_source": last_activity.get("source") if isinstance(last_activity, dict) else None,
        "mqtt_last_activity_age_seconds": mqtt_age_seconds,
        "mqtt_ttl_seconds": ttl_seconds,
        "mqtt_offline": offline,
    }


poller = StatePoller(
    spotify.current_playback,
    broker,
    settings.poll_interval_seconds,
    interval_strategy=lambda interval: spotify.next_poll_interval(interval, group="playback"),
    idle_interval_seconds=settings.spotify_idle_poll_interval_seconds,
    active_strategy=has_active_consumers,
    track_end_refresh_padding_seconds=settings.spotify_track_end_refresh_padding_seconds,
)
devices_poller = PeriodicPoller(
    lambda: refresh_devices_and_publish(spotify),
    settings.spotify_background_poll_interval_seconds,
    error_handler=broker.mark_spotify_error,
    interval_strategy=lambda interval: spotify.next_poll_interval(interval, group="devices"),
    idle_interval_seconds=settings.spotify_idle_poll_interval_seconds,
    active_strategy=has_active_consumers,
)
library_poller = PeriodicPoller(
    lambda: refresh_library_and_publish(spotify),
    settings.spotify_playlist_poll_interval_seconds,
    error_handler=broker.mark_spotify_error,
    interval_strategy=lambda interval: spotify.next_poll_interval(interval, group="playlists"),
    idle_interval_seconds=settings.spotify_idle_poll_interval_seconds,
    active_strategy=has_active_consumers,
)
art_cache = ArtCache(settings)
playlist_name_cache = PlaylistNameCache()
cached_devices: list[dict[str, Any]] | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    broker.set_mqtt_snapshot_factory(mqtt_knob_snapshot)
    broker.set_mqtt_config_factory(mqtt_knob_config)
    broker.set_mqtt_command_handler(handle_mqtt_command)
    broker.set_mqtt_request_handler(handle_mqtt_request)
    await broker.start()
    if spotify.spotify_configured:
        poller.start()
        devices_poller.start()
        library_poller.start()
    yield
    await library_poller.stop()
    await devices_poller.stop()
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
    consumer_active = has_active_consumers()
    consumers = broker.consumer_status(ttl_seconds=settings.active_consumer_ttl_seconds)
    idle_explanation = consumer_idle_explanation(consumers)
    playback_lower_bound = (
        settings.poll_interval_seconds
        if consumer_active
        else max(settings.poll_interval_seconds, settings.spotify_idle_poll_interval_seconds)
    )
    background_lower_bound = (
        settings.spotify_background_poll_interval_seconds if consumer_active else settings.spotify_idle_poll_interval_seconds
    )
    if not consumer_active:
        background_lower_bound = max(settings.spotify_background_poll_interval_seconds, background_lower_bound)
    playlist_lower_bound = (
        settings.spotify_playlist_poll_interval_seconds if consumer_active else settings.spotify_idle_poll_interval_seconds
    )
    if not consumer_active:
        playlist_lower_bound = max(settings.spotify_playlist_poll_interval_seconds, playlist_lower_bound)
    return {
        "ok": True,
        "spotify_configured": spotify.spotify_configured,
        "spotify_auth_configured": settings.spotify_auth_configured,
        "spotify_refresh_token_source": spotify.refresh_token_source,
        "mqtt_protocol": mqtt_protocol_payload(),
        "backend_capabilities": MQTT_KNOB_BACKEND_CAPABILITIES,
        "mqtt_enabled": settings.mqtt_enabled,
        "state_version": broker.version,
        "last_spotify_error": broker.last_spotify_error,
        "last_poll_at": broker.last_poll_at,
        "active_device_name": broker.current_state.device_name if broker.current_state else None,
        "target_device_name": target.device_name if target else None,
        "target_device_id": target.device_id if target else None,
        "target_readiness": cached_target_readiness(target),
        "playlist_name_cache": playlist_name_cache.status(),
        "art_cache": art_cache.status(),
        "consumers": consumers,
        "consumer_idle_explanation": idle_explanation,
        "rate_limit": spotify.rate_limit_statuses(
            {
                "playback": settings.poll_interval_seconds,
                "devices": settings.spotify_background_poll_interval_seconds,
                "playlists": settings.spotify_playlist_poll_interval_seconds,
                "library": settings.spotify_playlist_poll_interval_seconds,
                "commands": settings.poll_interval_seconds,
                "other": settings.poll_interval_seconds,
            }
        ),
        "polling": {
            "mode": "active" if consumer_active else "idle",
            "active_consumers_detected": consumer_active,
            "playback_active_interval_seconds": settings.poll_interval_seconds,
            "idle_interval_seconds": settings.spotify_idle_poll_interval_seconds,
            "background_active_interval_seconds": settings.spotify_background_poll_interval_seconds,
            "playlist_active_interval_seconds": settings.spotify_playlist_poll_interval_seconds,
            "playback_current_lower_bound_seconds": playback_lower_bound,
            "background_current_lower_bound_seconds": background_lower_bound,
            "playlist_current_lower_bound_seconds": playlist_lower_bound,
            "playback_effective_interval_seconds": spotify.next_poll_interval(playback_lower_bound, group="playback"),
            "background_effective_interval_seconds": spotify.next_poll_interval(background_lower_bound, group="devices"),
            "playlist_effective_interval_seconds": spotify.next_poll_interval(playlist_lower_bound, group="playlists"),
        },
        "mqtt_topics": broker.mqtt_topics() if settings.mqtt_enabled else None,
        "mqtt_availability": broker.last_mqtt_availability if settings.mqtt_enabled else None,
        "mqtt_availability_at": broker.last_mqtt_availability_at if settings.mqtt_enabled else None,
        "mqtt_commands": broker.mqtt_command_status() if settings.mqtt_enabled else None,
        "mqtt_retained": broker.retained_payload_status() if settings.mqtt_enabled else None,
    }


@app.get("/v1/debug/requests")
async def debug_requests(limit: int = Query(default=100, ge=0, le=1000)) -> dict[str, Any]:
    return telemetry.snapshot(periods_seconds=DEFAULT_PERIODS_SECONDS, recent_limit=limit)


@app.get("/v1/debug/events")
async def debug_events(
    period: str = Query(default="1h"),
    kind: str | None = None,
    request_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return telemetry.events(
        period_label=period,
        kind=kind,
        request_type=request_type,
        limit=limit,
        offset=offset,
        periods_seconds=DEFAULT_PERIODS_SECONDS,
    )


@app.get("/v1/debug/status")
async def debug_status(limit: int = Query(default=50, ge=0, le=500)) -> dict[str, Any]:
    return {
        "health": await health(),
        "requests": telemetry.snapshot(periods_seconds=DEFAULT_PERIODS_SECONDS, recent_limit=limit),
    }


@app.get("/debug", response_class=HTMLResponse)
async def debug_dashboard() -> HTMLResponse:
    return HTMLResponse(debug_dashboard_html())


def debug_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local Spotify Bridge Debug</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #101418;
      color: #e7ecef;
    }
    body {
      margin: 0;
      background: #101418;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }
    header {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 {
      font-size: 24px;
      margin: 0 0 4px;
      letter-spacing: 0;
    }
    .muted {
      color: #9da8af;
      font-size: 13px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .panel {
      border: 1px solid #29333b;
      background: #161d23;
      border-radius: 8px;
      padding: 14px;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 15px;
      letter-spacing: 0;
    }
    .metric {
      font-size: 26px;
      line-height: 1.2;
      font-weight: 650;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    button {
      background: #22303a;
      color: #e7ecef;
      border: 1px solid #354652;
      border-radius: 6px;
      padding: 8px 10px;
      cursor: pointer;
    }
    button.active {
      background: #2b6f61;
      border-color: #3f9f8d;
    }
    button.danger {
      background: #4a2630;
      border-color: #7a3d4a;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.5;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      table-layout: fixed;
    }
    th, td {
      border-bottom: 1px solid #29333b;
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: #b9c5cc;
      font-weight: 600;
    }
    code {
      color: #d7ecff;
      overflow-wrap: anywhere;
    }
    .table-scroll {
      overflow-x: auto;
    }
    .col-time {
      width: 120px;
    }
    .col-status {
      width: 92px;
    }
    .col-code {
      width: 70px;
    }
    .col-latency {
      width: 90px;
    }
    .col-action {
      width: 94px;
    }
    .ok {
      color: #7fd4a8;
    }
    .warn {
      color: #f3c969;
    }
    .error {
      color: #ff8b8b;
    }
    .clickable {
      cursor: pointer;
    }
    .clickable:hover {
      background: #1d2830;
    }
    .pager {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 10px;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
      max-height: 260px;
      overflow: auto;
      color: #d7ecff;
      font-size: 12px;
      line-height: 1.45;
    }
    .payload-row {
      display: none;
    }
    .payload-row.open {
      display: table-row;
    }
    .payload-box {
      background: #111820;
      border: 1px solid #29333b;
      border-radius: 6px;
      padding: 10px;
      max-width: 100%;
    }
    .payload-toggle {
      width: 100%;
      padding: 6px 8px;
      font-size: 12px;
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Local Spotify Bridge Debug</h1>
        <div class="muted" id="updated">Loading...</div>
      </div>
      <button id="refresh">Refresh</button>
    </header>

    <section class="grid">
      <div class="panel">
        <h2>Polling Mode</h2>
        <div class="metric" id="pollingMode">-</div>
        <div class="muted" id="pollingDetail"></div>
      </div>
      <div class="panel">
        <h2>Consumers</h2>
        <div class="metric" id="consumersActive">-</div>
        <div class="muted" id="consumersDetail"></div>
      </div>
      <div class="panel">
        <h2>Spotify Connection</h2>
        <div class="metric" id="spotifyConnection">-</div>
        <div class="muted" id="spotifyConnectionDetail"></div>
        <div class="toolbar" style="margin: 10px 0 0;">
          <button id="spotifyPair">Pair</button>
          <button class="danger" id="spotifyDisconnect">Disconnect</button>
        </div>
        <div class="muted" id="spotifyActionStatus"></div>
      </div>
      <div class="panel">
        <h2>Target Readiness</h2>
        <div class="metric" id="targetReadiness">-</div>
        <div class="muted" id="targetReadinessDetail"></div>
        <div class="muted" id="targetReadinessMeta"></div>
      </div>
      <div class="panel">
        <h2>Backend Contract</h2>
        <div class="metric" id="backendContract">-</div>
        <div class="muted" id="backendContractDetail"></div>
        <div class="muted" id="backendContractMeta"></div>
      </div>
      <div class="panel">
        <h2>Stored Events</h2>
        <div class="metric" id="storedEvents">-</div>
        <div class="muted" id="retention"></div>
      </div>
      <div class="panel">
        <h2>Last MQTT Command</h2>
        <div class="metric" id="lastCommand">-</div>
        <div class="muted" id="lastCommandDetail"></div>
      </div>
    </section>

    <section class="panel" style="margin-top: 16px;">
      <h2>Consumer Decision</h2>
      <div class="metric" id="consumerReason">-</div>
      <div class="muted" id="consumerReasonDetail"></div>
    </section>

    <section class="panel" style="margin-top: 16px;">
      <h2>MQTT Command / Request History</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th>Type</th>
              <th>Request ID</th>
              <th>Status</th>
              <th>Latency</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody id="mqttHistoryRows"></tbody>
        </table>
      </div>
    </section>

    <section class="panel" style="margin-top: 16px;">
      <h2>Retained MQTT Payloads</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Topic</th>
              <th>Kind</th>
              <th>Bytes</th>
              <th>Status</th>
              <th>Updated</th>
              <th>Preview</th>
            </tr>
          </thead>
          <tbody id="mqttRetainedRows"></tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="toolbar" id="periods"></div>
      <div class="grid">
        <div>
          <h2>Spotify API Requests</h2>
          <table>
            <thead><tr><th>Type</th><th>Count</th><th>OK</th><th>Errors</th><th>Avg ms</th><th>Last</th></tr></thead>
            <tbody id="spotifyRows"></tbody>
          </table>
        </div>
        <div>
          <h2>MQTT Postings</h2>
          <table>
            <thead><tr><th>Topic</th><th>Count</th><th>Published</th><th>Skipped</th><th>Last</th></tr></thead>
            <tbody id="mqttRows"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="panel" id="detailPanel" style="display: none; margin-top: 16px;">
      <h2 id="detailTitle">Details</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th class="col-time">Time</th>
              <th class="col-status">Status</th>
              <th class="col-code">Code</th>
              <th class="col-latency">Latency</th>
              <th>Details</th>
              <th class="col-action">Payload</th>
            </tr>
          </thead>
          <tbody id="detailRows"></tbody>
        </table>
      </div>
      <div class="pager">
        <button id="detailPrev">Previous</button>
        <div class="muted" id="detailPage"></div>
        <button id="detailNext">Next</button>
      </div>
    </section>

    <section class="panel" style="margin-top: 16px;">
      <h2 id="recentTitle">Recent Events</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th class="col-time">Time</th>
              <th>Kind</th>
              <th>Type</th>
              <th class="col-status">Status</th>
              <th>Details</th>
              <th class="col-action">Payload</th>
            </tr>
          </thead>
          <tbody id="recentRows"></tbody>
        </table>
      </div>
      <div class="pager">
        <button id="recentPrev">Previous</button>
        <div class="muted" id="recentPage"></div>
        <button id="recentNext">Next</button>
      </div>
    </section>
  </main>
  <script>
    let selectedPeriod = "1h";
    let latestPayload = null;
    let recentOffset = 0;
    const recentLimit = 25;
    let detailFilter = null;
    let detailOffset = 0;
    const detailLimit = 25;

    function cell(text, className) {
      const td = document.createElement("td");
      if (className) td.className = className;
      td.textContent = text == null ? "-" : String(text);
      return td;
    }

    function codeCell(text) {
      const td = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = text == null ? "-" : String(text);
      td.appendChild(code);
      return td;
    }

    function yesNo(value) {
      if (value === true) return "yes";
      if (value === false) return "no";
      return "-";
    }

    function renderRows(target, data, kind) {
      target.replaceChildren();
      const entries = Object.entries(data.by_type || {});
      if (!entries.length) {
        const tr = document.createElement("tr");
        const td = cell("No events in this period");
        td.colSpan = kind === "spotify" ? 6 : 5;
        tr.appendChild(td);
        target.appendChild(tr);
        return;
      }
      for (const [name, row] of entries) {
        const tr = document.createElement("tr");
        tr.className = "clickable";
        tr.title = "Open latest events for this type";
        tr.onclick = () => openDetails(kind === "spotify" ? "spotify_api_request" : "mqtt_posting", name);
        tr.appendChild(codeCell(name));
        tr.appendChild(cell(row.count));
        tr.appendChild(cell(row.ok, row.ok ? "ok" : ""));
        if (kind === "spotify") {
          tr.appendChild(cell(row.errors, row.errors ? "error" : ""));
          tr.appendChild(cell(row.avg_latency_ms));
          tr.appendChild(cell(row.last_status_code || row.last_status || "-"));
        } else {
          tr.appendChild(cell(row.skipped, row.skipped ? "warn" : ""));
          tr.appendChild(cell(row.last_status || "-"));
        }
        target.appendChild(tr);
      }
    }

    function render(payload) {
      latestPayload = payload;
      document.getElementById("updated").textContent = "Updated " + new Date(payload.requests.generated_at).toLocaleString();
      const health = payload.health;
      document.getElementById("pollingMode").textContent = health.polling.mode;
      document.getElementById("pollingDetail").textContent =
        "Playback " + health.polling.playback_effective_interval_seconds + "s, background " +
        health.polling.background_effective_interval_seconds + "s, playlists " +
        health.polling.playlist_effective_interval_seconds + "s";
      document.getElementById("consumersActive").textContent = health.polling.active_consumers_detected ? "active" : "idle";
      document.getElementById("consumersDetail").textContent =
        "WS " + health.consumers.websocket_count + ", MQTT " + (health.consumers.mqtt_active ? "active" : "inactive");
      const tokenSource = health.spotify_refresh_token_source || "none";
      document.getElementById("spotifyConnection").textContent = health.spotify_configured ? "connected" : "not paired";
      document.getElementById("spotifyConnection").className = "metric " + (health.spotify_configured ? "ok" : "warn");
      document.getElementById("spotifyConnectionDetail").textContent =
        "token source " + tokenSource + ", auth app " + (health.spotify_auth_configured ? "configured" : "missing");
      document.getElementById("spotifyDisconnect").disabled = tokenSource === "none";
      const readiness = health.target_readiness || {};
      const risks = Array.isArray(readiness.risks) ? readiness.risks : [];
      const readinessReady = readiness.ready_for_live_control === true;
      const readinessSafe = readiness.safe_for_live_control === true;
      const readinessLabel = readinessReady ? "ready" : (readinessSafe ? "attention" : "blocked");
      document.getElementById("targetReadiness").textContent = readinessLabel;
      document.getElementById("targetReadiness").className =
        "metric " + (readinessReady ? "ok" : (readinessSafe ? "warn" : "error"));
      document.getElementById("targetReadinessDetail").textContent =
        (readiness.resolved_device_id || "no target") + ", risks " + (risks.length ? risks.join(", ") : "none");
      document.getElementById("targetReadinessMeta").textContent =
        "active " + yesNo(readiness.active) + ", volume " + yesNo(readiness.volume_control_supported) +
        ", checked " + (readiness.checked_at || "-");
      const capabilities = health.backend_capabilities || {};
      const protocol = health.mqtt_protocol || {};
      const library = capabilities.library || {};
      const devices = capabilities.devices || {};
      const art = capabilities.art || {};
      const architecture = capabilities.architecture || {};
      document.getElementById("backendContract").textContent = capabilities.backend || "-";
      document.getElementById("backendContract").className = "metric " + (capabilities.backend ? "ok" : "warn");
      document.getElementById("backendContractDetail").textContent =
        "transport " + (capabilities.transport || "-") + ", schema " + (protocol.schema_version || "-") +
        ", role " + (architecture.role || "-");
      document.getElementById("backendContractMeta").textContent =
        "library recent " + yesNo(library.recent_tracks) + ", devices readiness " + yesNo(devices.readiness) +
        ", RGB565 art " + yesNo(art.rgb565) + ", on-device direct Spotify " + yesNo(architecture.direct_spotify_on_device);
      const idle = health.consumer_idle_explanation || {};
      const idleAge = idle.mqtt_last_activity_age_seconds == null ? "no MQTT activity" : Math.round(idle.mqtt_last_activity_age_seconds) + "s ago";
      document.getElementById("consumerReason").textContent = idle.reason || "-";
      document.getElementById("consumerReasonDetail").textContent =
        "WS " + (idle.websocket_count || 0) + ", MQTT " + (idle.mqtt_active ? "active" : "inactive") +
        ", source " + (idle.mqtt_last_activity_source || "-") + ", " + idleAge +
        ", TTL " + (idle.mqtt_ttl_seconds == null ? "-" : Math.round(idle.mqtt_ttl_seconds) + "s");
      document.getElementById("storedEvents").textContent = payload.requests.stored_events;
      document.getElementById("retention").textContent = "Retention " + Math.round(payload.requests.retention_seconds / 3600) + "h";
      const commandStatus = health.mqtt_commands || {};
      const lastCommand = commandStatus.last_command || {};
      const lastResult = commandStatus.last_result || {};
      document.getElementById("lastCommand").textContent = lastCommand.type || "-";
      const resultText = lastResult.ok === true ? "ok" : (lastResult.ok === false ? "failed" : "pending");
      const latency = lastResult.latency_ms == null ? "" : ", " + Math.round(lastResult.latency_ms) + "ms";
      const replay = lastResult.idempotent_replay ? ", replay" : "";
      const error = lastResult.error ? ", " + lastResult.error : "";
      document.getElementById("lastCommandDetail").textContent =
        (lastCommand.request_id || "no request id") + " - " + resultText + latency + replay + error;
      const historyRows = document.getElementById("mqttHistoryRows");
      historyRows.replaceChildren();
      const recentRpc = commandStatus.recent || [];
      if (!recentRpc.length) {
        const tr = document.createElement("tr");
        const td = cell("No MQTT commands or requests yet");
        td.colSpan = 6;
        tr.appendChild(td);
        historyRows.appendChild(tr);
      } else {
        for (const item of recentRpc) {
          const tr = document.createElement("tr");
          const status = item.ok === true ? "ok" : (item.ok === false ? "failed" : "pending");
          const resultBits = [];
          if (item.idempotent_replay) resultBits.push("replay");
          if (item.error) resultBits.push(item.error);
          if (item.state_version != null) resultBits.push("state " + item.state_version);
          if (item.published_topic) resultBits.push(item.published_topic);
          if (item.published_version != null) resultBits.push("v" + item.published_version);
          tr.appendChild(cell(item.label || "-"));
          tr.appendChild(cell(item.type || "-"));
          tr.appendChild(codeCell(item.request_id || "-"));
          tr.appendChild(cell(status, item.ok === false ? "error" : (item.idempotent_replay ? "warn" : "ok")));
          tr.appendChild(cell(item.latency_ms == null ? "-" : Math.round(item.latency_ms) + "ms"));
          tr.appendChild(cell(resultBits.join(", ") || "-"));
          historyRows.appendChild(tr);
        }
      }
      const retainedRows = document.getElementById("mqttRetainedRows");
      retainedRows.replaceChildren();
      const retained = health.mqtt_retained || [];
      if (!retained.length) {
        const tr = document.createElement("tr");
        const td = cell("No retained MQTT payloads published in this process yet");
        td.colSpan = 6;
        tr.appendChild(td);
        retainedRows.appendChild(tr);
      } else {
        for (const item of retained) {
          const tr = document.createElement("tr");
          const status = item.published ? "published" : "duplicate";
          tr.appendChild(codeCell(item.topic_key || item.topic || "-"));
          tr.appendChild(cell(item.payload_kind || "-"));
          tr.appendChild(cell(item.payload_bytes == null ? "-" : item.payload_bytes));
          tr.appendChild(cell(status, item.published ? "ok" : "warn"));
          tr.appendChild(cell(item.updated_at ? new Date(item.updated_at).toLocaleTimeString() : "-"));
          tr.appendChild(codeCell(item.preview || item.fingerprint || "-"));
          retainedRows.appendChild(tr);
        }
      }

      const periods = document.getElementById("periods");
      periods.replaceChildren();
      for (const label of Object.keys(payload.requests.periods)) {
        const button = document.createElement("button");
        button.textContent = label;
        button.className = label === selectedPeriod ? "active" : "";
        button.onclick = () => {
          selectedPeriod = label;
          recentOffset = 0;
          detailOffset = 0;
          render(latestPayload);
          loadRecent();
          if (detailFilter) loadDetails();
        };
        periods.appendChild(button);
      }

      const period = payload.requests.periods[selectedPeriod] || payload.requests.periods["1h"];
      renderRows(document.getElementById("spotifyRows"), period.spotify_api_requests, "spotify");
      renderRows(document.getElementById("mqttRows"), period.mqtt_postings, "mqtt");

      loadRecent();
    }

    function eventDetails(event) {
      const details = [];
      if (event.status_code) details.push("status " + event.status_code);
      if (event.latency_ms) details.push(Math.round(event.latency_ms) + "ms");
      if (event.payload_bytes) details.push(event.payload_bytes + " bytes");
      if (event.wait_seconds) details.push("wait " + event.wait_seconds + "s");
      if (event.retry_after) details.push("retry-after " + event.retry_after);
      if (event.error) details.push(event.error);
      return details.join(", ");
    }

    function payloadButtonCell(event, payloadRow) {
      const td = document.createElement("td");
      const button = document.createElement("button");
      button.className = "payload-toggle";
      button.textContent = event.detail ? "View" : "Empty";
      button.disabled = !event.detail;
      button.onclick = () => {
        payloadRow.classList.toggle("open");
        button.textContent = payloadRow.classList.contains("open") ? "Hide" : "View";
      };
      td.appendChild(button);
      return td;
    }

    function appendPayloadRows(target, event, metadataCells, columnCount) {
      const row = document.createElement("tr");
      const payloadRow = document.createElement("tr");
      payloadRow.className = "payload-row";
      for (const node of metadataCells) row.appendChild(node);
      row.appendChild(payloadButtonCell(event, payloadRow));

      const payloadCell = document.createElement("td");
      payloadCell.colSpan = columnCount;
      const box = document.createElement("div");
      box.className = "payload-box";
      const pre = document.createElement("pre");
      pre.textContent = event.detail || "";
      box.appendChild(pre);
      payloadCell.appendChild(box);
      payloadRow.appendChild(payloadCell);
      target.appendChild(row);
      target.appendChild(payloadRow);
    }

    async function fetchEvents({kind = null, requestType = null, offset = 0, limit = 25} = {}) {
      const params = new URLSearchParams({period: selectedPeriod, offset, limit});
      if (kind) params.set("kind", kind);
      if (requestType) params.set("request_type", requestType);
      const response = await fetch("/v1/debug/events?" + params.toString());
      return await response.json();
    }

    async function loadRecent() {
      const payload = await fetchEvents({offset: recentOffset, limit: recentLimit});
      document.getElementById("recentTitle").textContent = "Recent Events (" + selectedPeriod + ")";
      const recent = document.getElementById("recentRows");
      recent.replaceChildren();
      for (const event of payload.items) {
        appendPayloadRows(recent, event, [
          cell(new Date(event.at).toLocaleTimeString()),
          cell(event.kind),
          codeCell(event.request_type),
          cell(event.status, event.status === "error" ? "error" : event.status === "skipped" ? "warn" : "ok"),
          cell(eventDetails(event))
        ], 6);
      }
      document.getElementById("recentPage").textContent = payload.total
        ? (recentOffset + 1) + "-" + Math.min(recentOffset + recentLimit, payload.total) + " of " + payload.total
        : "0 of 0";
      document.getElementById("recentPrev").disabled = recentOffset === 0;
      document.getElementById("recentNext").disabled = payload.next_offset == null;
    }

    async function openDetails(kind, requestType) {
      detailFilter = {kind, requestType};
      detailOffset = 0;
      await loadDetails();
    }

    async function loadDetails() {
      if (!detailFilter) return;
      const payload = await fetchEvents({
        kind: detailFilter.kind,
        requestType: detailFilter.requestType,
        offset: detailOffset,
        limit: detailLimit
      });
      document.getElementById("detailPanel").style.display = "block";
      document.getElementById("detailTitle").textContent =
        detailFilter.kind + " / " + detailFilter.requestType + " (" + selectedPeriod + ")";
      const rows = document.getElementById("detailRows");
      rows.replaceChildren();
      for (const event of payload.items) {
        appendPayloadRows(rows, event, [
          cell(new Date(event.at).toLocaleString()),
          cell(event.status, event.status === "error" ? "error" : event.status === "skipped" ? "warn" : "ok"),
          cell(event.status_code || "-"),
          cell(event.latency_ms ? Math.round(event.latency_ms) + "ms" : "-"),
          cell(eventDetails(event))
        ], 6);
      }
      document.getElementById("detailPage").textContent = payload.total
        ? (detailOffset + 1) + "-" + Math.min(detailOffset + detailLimit, payload.total) + " of " + payload.total
        : "0 of 0";
      document.getElementById("detailPrev").disabled = detailOffset === 0;
      document.getElementById("detailNext").disabled = payload.next_offset == null;
    }

    async function refresh() {
      const response = await fetch("/v1/debug/status?limit=100");
      render(await response.json());
    }

    async function startSpotifyPairing() {
      const status = document.getElementById("spotifyActionStatus");
      status.textContent = "Requesting Spotify authorization URL...";
      const response = await fetch("/v1/auth/login");
      const payload = await response.json();
      if (!response.ok) {
        status.textContent = payload.detail || "Spotify pairing is not configured.";
        return;
      }
      status.textContent = "Opening Spotify authorization...";
      window.open(payload.authorize_url, "_blank", "noopener");
    }

    async function disconnectSpotify() {
      const status = document.getElementById("spotifyActionStatus");
      status.textContent = "Disconnecting Spotify...";
      const response = await fetch("/v1/auth/token", {method: "DELETE"});
      const payload = await response.json();
      status.textContent = payload.message || (response.ok ? "Spotify disconnected." : "Disconnect failed.");
      await refresh();
    }

    document.getElementById("refresh").onclick = refresh;
    document.getElementById("spotifyPair").onclick = startSpotifyPairing;
    document.getElementById("spotifyDisconnect").onclick = disconnectSpotify;
    document.getElementById("recentPrev").onclick = () => {
      recentOffset = Math.max(0, recentOffset - recentLimit);
      loadRecent();
    };
    document.getElementById("recentNext").onclick = () => {
      recentOffset += recentLimit;
      loadRecent();
    };
    document.getElementById("detailPrev").onclick = () => {
      detailOffset = Math.max(0, detailOffset - detailLimit);
      loadDetails();
    };
    document.getElementById("detailNext").onclick = () => {
      detailOffset += detailLimit;
      loadDetails();
    };
    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>"""


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
    except HTTPException:
        raise
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
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_spotify_error(exc) from exc

    return {
        "message": "Refresh token saved. The bridge is configured now; no restart is required.",
        "refresh_token": token.get("refresh_token"),
        "access_token_expires_in": token.get("expires_in"),
        "scope": token.get("scope"),
        "token_type": token.get("token_type"),
    }


@app.delete("/v1/auth/token")
async def auth_disconnect(
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    env_refresh_token_configured = client.disconnect_runtime_credentials()
    broker.current_state = None
    await publish_mqtt_status(command_type="disconnect_spotify", command_ok=not env_refresh_token_configured)
    message = (
        "Persisted Spotify token cleared, but SPOTIFY_REFRESH_TOKEN is still configured in the environment."
        if env_refresh_token_configured
        else "Spotify disconnected. Pair again from /v1/auth/login."
    )
    return {
        "message": message,
        "persisted_refresh_token_cleared": True,
        "env_refresh_token_configured": env_refresh_token_configured,
        "spotify_refresh_token_source": client.refresh_token_source,
        "spotify_configured": client.spotify_configured,
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
    readiness = None
    if target and client.spotify_configured:
        try:
            readiness = await target_device_readiness(client, target, refresh=True)
            resolved_device_id = explicit_device_id(readiness.get("resolved_device_id"))
        except Exception as exc:
            broker.mark_spotify_error(exc)
    return {
        "target": target.model_dump(mode="json") if target else None,
        "resolved_device_id": resolved_device_id,
        "readiness": readiness,
    }


@app.get("/v1/target/verify")
async def verify_target_for_live_control(
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
    state_store: Annotated[RuntimeStore, Depends(runtime_store)] = store,
) -> dict[str, Any]:
    target = state_store.get_target_device()
    if target is None:
        readiness = target_device_readiness_from_devices(None, None, checked_at=datetime.now(UTC).isoformat())
        raise HTTPException(
            status_code=409,
            detail={"message": "No target device is configured for live control.", "readiness": readiness},
        )
    if not client.spotify_configured:
        readiness = target_device_readiness_from_devices(target, None, checked_at=datetime.now(UTC).isoformat())
        raise HTTPException(
            status_code=409,
            detail={"message": "Spotify credentials are required to verify a live target.", "readiness": readiness},
        )

    try:
        readiness = await target_device_readiness(client, target, refresh=True)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc
    if not readiness.get("ready_for_live_control", False):
        raise HTTPException(
            status_code=409,
            detail={"message": "Target device is not ready for live control.", "readiness": readiness},
        )
    return {
        "ok": True,
        "target": target.model_dump(mode="json"),
        "resolved_device_id": explicit_device_id(readiness.get("resolved_device_id")),
        "readiness": readiness,
    }


class TargetNotReadyForLiveControl(ValueError):
    def __init__(self, command_type: str, readiness: dict[str, Any]) -> None:
        risks = readiness.get("risks", [])
        risk_text = ",".join(str(risk) for risk in risks) if isinstance(risks, list) and risks else "unknown"
        super().__init__(f"{command_type} target is not ready for live control: {risk_text}")
        self.command_type = command_type
        self.readiness = readiness


async def verified_live_control_device_id(
    client: SpotifyClient,
    explicit_device_id_value: Any,
    *,
    command_type: str,
) -> str | None:
    explicit = explicit_device_id(explicit_device_id_value)
    if explicit:
        return explicit
    target = store.get_target_device()
    if target is None:
        return None
    readiness = await target_device_readiness(client, target, refresh=True)
    if not readiness.get("ready_for_live_control", False):
        raise TargetNotReadyForLiveControl(command_type, readiness)
    return explicit_device_id(readiness.get("resolved_device_id"))


async def rest_live_control_device_id(
    client: SpotifyClient,
    explicit_device_id_value: Any,
    *,
    command_type: str,
) -> str | None:
    try:
        return await verified_live_control_device_id(client, explicit_device_id_value, command_type=command_type)
    except TargetNotReadyForLiveControl as exc:
        await publish_mqtt_status(command_type=command_type, command_ok=False, command_error=str(exc), force_publish=True)
        raise HTTPException(status_code=409, detail={"message": str(exc), "readiness": exc.readiness}) from exc


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
        await publish_mqtt_status(command_type="clear_target", command_ok=True, force_publish=True)
        return {"target": None, "resolved_device_id": None}

    target = TargetDevice(device_id=command.device_id, device_name=command.device_name)
    resolved_device_id = command.device_id
    readiness: dict[str, Any] | None = None
    transfer_succeeded = False
    try:
        if client.spotify_configured:
            readiness = await target_device_readiness(client, target, refresh=True)
            resolved_device_id = explicit_device_id(readiness.get("resolved_device_id"))
        if command.transfer_playback and not client.spotify_configured:
            raise SpotifyNotConfigured(
                "Spotify credentials are required to transfer playback while setting target."
            )
        if command.transfer_playback and not (readiness or {}).get("safe_for_live_control", False):
            raise HTTPException(status_code=409, detail={"message": "Target device is not safe for live control.", "readiness": readiness})
        if command.transfer_playback and resolved_device_id:
            await client.transfer_playback(resolved_device_id, command.play)
            transfer_succeeded = True
    except Exception as exc:
        raise translate_spotify_error(exc) from exc

    state_store.set_target_device(target)
    try:
        if transfer_succeeded:
            await publish_mqtt_status(command_type="transfer", command_ok=True)
            await refresh_after_successful_command(
                client,
                follow_up_delays=settings.command_followup_refresh_delays_for("transfer"),
            )
            await refresh_devices_after_successful_command(client)
        else:
            if client.spotify_configured:
                await refresh_devices_and_publish(client)
            await publish_mqtt_status(command_type="set_target", command_ok=True, force_publish=True)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc
    return {
        "target": target.model_dump(mode="json"),
        "resolved_device_id": resolved_device_id,
        "readiness": readiness,
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
        device_id = await rest_live_control_device_id(client, command.device_id, command_type="play")
        await client.play(body=body, device_id=device_id)
        await publish_mqtt_status(command_type="play", command_ok=True)
        await refresh_after_successful_command(client, follow_up_delays=settings.command_followup_refresh_delays_for("play"))
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/pause", status_code=204)
async def pause(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.pause(device_id=await rest_live_control_device_id(client, device_id, command_type="pause"))
        await publish_mqtt_status(command_type="pause", command_ok=True)
        await refresh_after_successful_command(client, follow_up_delays=settings.command_followup_refresh_delays_for("pause"))
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/next", status_code=204)
async def next_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.next_track(device_id=await rest_live_control_device_id(client, device_id, command_type="next"))
        broker.mark_forward_transition_expected()
        await publish_mqtt_status(command_type="next", command_ok=True)
        await refresh_after_successful_command(client, follow_up_delays=settings.command_followup_refresh_delays_for("next"))
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/previous", status_code=204)
async def previous_track(
    device_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        await client.previous_track(device_id=await rest_live_control_device_id(client, device_id, command_type="previous"))
        await publish_mqtt_status(command_type="previous", command_ok=True)
        await refresh_after_successful_command(client, follow_up_delays=settings.command_followup_refresh_delays_for("previous"))
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/transfer", status_code=204)
async def transfer_playback(
    command: TransferPlaybackCommand,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        readiness = await target_device_readiness(client, TargetDevice(device_id=command.device_id), refresh=True)
        if not readiness.get("safe_for_live_control", False):
            raise HTTPException(status_code=409, detail={"message": "Target device is not safe for live control.", "readiness": readiness})
        await client.transfer_playback(command.device_id, command.play)
        store.set_target_device(TargetDevice(device_id=command.device_id))
        await publish_mqtt_status(command_type="transfer", command_ok=True)
        await refresh_after_successful_command(client, follow_up_delays=settings.command_followup_refresh_delays_for("transfer"))
        await refresh_devices_after_successful_command(client)
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.post("/v1/control/seek", status_code=204)
async def seek(command: SeekCommand, client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify):
    try:
        await client.seek(
            command.position_ms,
            await rest_live_control_device_id(client, command.device_id, command_type="seek"),
        )
        await publish_mqtt_status(command_type="seek", command_ok=True)
        await refresh_after_successful_command(client)
    except HTTPException:
        raise
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
        await publish_mqtt_status(command_type="volume_set", command_ok=True)
        await refresh_after_successful_command(client)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/devices")
async def devices(client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify):
    try:
        device_list = await current_devices(client, refresh=True)
        return {"devices": device_list}
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


@app.get("/v1/recent-tracks")
async def recent_tracks(
    limit: int = Query(default=50, ge=1, le=50),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return await client.recently_played_tracks(limit=limit)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/library/recent-tracks")
async def compact_recent_library_tracks(
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
):
    try:
        return compact_recent_tracks(await client.recently_played_tracks(limit=50), limit=limit, offset=offset).model_dump(mode="json")
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


@app.get("/v1/knob/library/playlists")
async def knob_library_playlists(
    request_id: str | None = None,
    client: Annotated[SpotifyClient, Depends(spotify_client)] = spotify,
) -> dict[str, Any]:
    try:
        return await build_full_playlists_payload(client, request_id=request_id)
    except Exception as exc:
        raise translate_spotify_error(exc) from exc


@app.get("/v1/knob/library/page")
async def knob_library_page(
    kind: str = Query(pattern="^(playlists|saved_tracks|recent_tracks|playlist_tracks|devices)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=3, ge=1, le=10),
    page: int = Query(default=0, ge=0, le=3),
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
        return await build_devices_payload(client, request_id=request_id, offset=offset, limit=limit, refresh=True)
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
    force_publish: bool = False,
) -> bool:
    published = await broker.publish_if_changed(await client.current_playback(), force=force_publish)
    for delay in follow_up_delays:
        asyncio.create_task(delayed_refresh_and_publish(client, delay))
    return published


async def refresh_after_successful_command(
    client: SpotifyClient,
    *,
    follow_up_delays: tuple[float, ...] = (),
) -> bool:
    try:
        return await refresh_and_publish(client, follow_up_delays=follow_up_delays, force_publish=True)
    except Exception as exc:
        broker.mark_spotify_error(exc)
        return False


async def refresh_devices_after_successful_command(client: SpotifyClient) -> None:
    try:
        await refresh_devices_and_publish(client)
    except Exception as exc:
        broker.mark_spotify_error(exc)


async def delayed_refresh_and_publish(client: SpotifyClient, delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await broker.publish_if_changed(await client.current_playback())
    except Exception as exc:
        broker.mark_spotify_error(exc)


async def mqtt_knob_snapshot(version: int, state, force_publish: bool = False) -> dict[str, Any]:
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
    await publish_mqtt_status(force_publish=force_publish)
    await broker.publish_mqtt_retained(
        "control_state",
        mqtt_control_state(version, state, context_name=context_name),
        force=force_publish,
    )
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
    return mqtt_knob_config_payload(
        device_id=settings.mqtt_knob_device_id,
        qos=settings.mqtt_qos,
        topics=broker.mqtt_topics(),
        base_url=mqtt_base_url(),
        art_options=art_options,
    )


def mqtt_control_state(version: int, state: PlaybackSnapshot | None, *, context_name: str | None = None) -> dict[str, Any]:
    return mqtt_control_state_payload(version, state, context_name=context_name)


def track_id_from_command_or_state(command: dict[str, Any], state: PlaybackSnapshot | None) -> str:
    track_id = command.get("track_id") or command.get("id")
    if isinstance(track_id, str) and track_id:
        return track_id

    track_uri = command.get("track_uri") or command.get("uri")
    if isinstance(track_uri, str) and track_uri.startswith("spotify:track:"):
        parsed_track_id = track_uri.rsplit(":", 1)[-1]
        if parsed_track_id:
            return parsed_track_id

    if state and state.item_id:
        return state.item_id
    if state and state.item_uri and state.item_uri.startswith("spotify:track:"):
        parsed_track_id = state.item_uri.rsplit(":", 1)[-1]
        if parsed_track_id:
            return parsed_track_id

    raise ValueError("track save command requires track_id, track_uri, or current track state.")


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


def mqtt_status_payload(
    command_type: str | None = None,
    command_request_id: str | None = None,
    command_pending: bool | None = None,
    command_ok: bool | None = None,
    command_error: str | None = None,
    command_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command_pulse = None
    if command_type:
        command_pulse = {
            "type": command_type,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        if command_request_id:
            command_pulse["request_id"] = command_request_id
        if command_ok is not None:
            command_pulse["ok"] = command_ok
        if command_error:
            command_pulse["error"] = command_error
        if command_metadata:
            for key in (
                "ignored",
                "reason",
                "playback_affecting",
                "state_version",
                "published_state",
                "state_refresh_ok",
                "state_publish_forced",
            ):
                if key in command_metadata:
                    command_pulse[key] = command_metadata[key]
    spotify_configured = bool(getattr(spotify, "spotify_configured", False))
    return status_payload(
        version=broker.version,
        spotify_configured=spotify_configured,
        last_poll_at=broker.last_poll_at,
        last_error=broker.last_spotify_error,
        current_state=broker.current_state,
        target=store.get_target_device(),
        mqtt_connected=settings.mqtt_enabled,
        command_pending=bool(broker.pending_mqtt_command) if command_pending is None else command_pending,
        command_pulse=command_pulse,
        target_readiness=cached_target_readiness(store.get_target_device()),
    )


async def publish_mqtt_status(
    command_type: str | None = None,
    command_request_id: str | None = None,
    command_pending: bool | None = None,
    command_ok: bool | None = None,
    command_error: str | None = None,
    command_metadata: dict[str, Any] | None = None,
    force_publish: bool = False,
) -> None:
    await broker.publish_mqtt_retained(
        "status",
        mqtt_status_payload(
            command_type=command_type,
            command_request_id=command_request_id,
            command_pending=command_pending,
            command_ok=command_ok,
            command_error=command_error,
            command_metadata=command_metadata,
        ),
        force=force_publish,
    )


async def refresh_devices_and_publish(client: SpotifyClient) -> dict[str, Any]:
    payload = await build_devices_payload(client, request_id=None, offset=0, limit=10, refresh=True)
    await broker.publish_mqtt_retained("devices", payload)
    return payload


async def refresh_library_and_publish(client: SpotifyClient) -> None:
    root_payload = await build_library_root_payload(client)
    await broker.publish_mqtt_retained("library/root", root_payload)
    playlists_payload = await build_full_playlists_payload(client)
    await broker.publish_mqtt_retained("library/playlists", playlists_payload)
    page_payload = await build_library_page_payload(
        client,
        request_id=None,
        page=0,
        kind="playlists",
        offset=0,
        limit=3,
    )
    await broker.publish_mqtt_retained("library/page", page_payload)


async def current_devices(client: SpotifyClient, *, refresh: bool = False) -> list[dict[str, Any]]:
    global cached_devices
    if cached_devices is not None and not refresh:
        return cached_devices

    payload = await client.devices()
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    cached_devices = devices
    return devices


def active_device_id(devices: list[dict[str, Any]]) -> str | None:
    for device in devices:
        if device.get("is_active"):
            device_id = device.get("id")
            return device_id if isinstance(device_id, str) else None
    return None


def target_device_from_list(devices: list[dict[str, Any]], target: TargetDevice) -> dict[str, Any] | None:
    if target.device_id:
        for device in devices:
            if device.get("id") == target.device_id:
                return device
    if target.device_name:
        target_name = target.device_name.casefold()
        for device in devices:
            if str(device.get("name", "")).casefold() == target_name:
                return device
    return None


def target_device_readiness_from_devices(
    target: TargetDevice | None,
    devices: list[dict[str, Any]] | None,
    *,
    checked_at: str,
) -> dict[str, Any]:
    if target is None:
        return {
            "checked_at": checked_at,
            "last_update_at": checked_at,
            "source": "cached_devices" if devices is not None else "unavailable",
            "target": None,
            "resolved_device_id": None,
            "safe_for_live_control": False,
            "ready_for_live_control": False,
            "active": False,
            "restricted": False,
            "volume_control_supported": False,
            "muted_or_zero_volume": False,
            "risks": ["target_not_configured"],
        }
    if devices is None:
        return {
            "checked_at": checked_at,
            "last_update_at": checked_at,
            "source": "unavailable",
            "target": target.model_dump(mode="json"),
            "resolved_device_id": None,
            "safe_for_live_control": False,
            "ready_for_live_control": False,
            "active": False,
            "restricted": False,
            "volume_control_supported": False,
            "muted_or_zero_volume": False,
            "risks": ["devices_not_cached"],
            "device": None,
        }

    device = target_device_from_list(devices, target)
    risks: list[str] = []
    if device is None:
        risks.append("target_not_found")
    device_id = device.get("id") if isinstance(device, dict) else None
    if not device_id:
        risks.append("missing_device_id")
    if isinstance(device, dict) and device.get("is_restricted"):
        risks.append("restricted_device")
    if isinstance(device, dict) and not device.get("is_active"):
        risks.append("inactive_device")
    if isinstance(device, dict) and not device.get("supports_volume"):
        risks.append("volume_unavailable")
    muted_or_zero_volume = isinstance(device, dict) and device.get("supports_volume") and device.get("volume_percent") == 0
    if muted_or_zero_volume:
        risks.append("zero_volume")

    safe_for_live_control = bool(device_id) and "restricted_device" not in risks
    ready_for_live_control = safe_for_live_control and not any(
        risk in risks for risk in ("inactive_device", "volume_unavailable", "zero_volume")
    )
    return {
        "checked_at": checked_at,
        "last_update_at": checked_at,
        "source": "cached_devices",
        "target": target.model_dump(mode="json"),
        "resolved_device_id": device_id if isinstance(device_id, str) else None,
        "safe_for_live_control": safe_for_live_control,
        "ready_for_live_control": ready_for_live_control,
        "active": bool(device.get("is_active")) if isinstance(device, dict) else False,
        "restricted": bool(device.get("is_restricted")) if isinstance(device, dict) else False,
        "volume_control_supported": bool(device.get("supports_volume")) if isinstance(device, dict) else False,
        "muted_or_zero_volume": bool(muted_or_zero_volume),
        "risks": risks,
        "device": {
            "id": device.get("id"),
            "name": device.get("name"),
            "type": device.get("type"),
            "is_active": bool(device.get("is_active")),
            "is_restricted": bool(device.get("is_restricted")),
            "volume_control_supported": bool(device.get("supports_volume")),
            "volume_percent": device.get("volume_percent"),
        }
        if isinstance(device, dict)
        else None,
    }


def cached_target_readiness(target: TargetDevice | None) -> dict[str, Any]:
    return target_device_readiness_from_devices(
        target,
        cached_devices,
        checked_at=datetime.now(UTC).isoformat(),
    )


async def target_device_readiness(
    client: SpotifyClient,
    target: TargetDevice | None,
    *,
    refresh: bool,
) -> dict[str, Any]:
    checked_at = datetime.now(UTC).isoformat()
    devices = await current_devices(client, refresh=refresh)
    return target_device_readiness_from_devices(target, devices, checked_at=checked_at)


async def build_library_root_payload(client: SpotifyClient) -> dict[str, Any]:
    playlists_payload = await client.playlists(limit=1, offset=0)
    saved_payload = await client.saved_tracks(limit=1, offset=0)
    recent_payload = await client.recently_played_tracks(limit=1)
    devices = cached_devices or []
    return library_root_payload(
        version=broker.version,
        playlist_total=playlists_payload.get("total") if isinstance(playlists_payload, dict) else None,
        saved_total=saved_payload.get("total") if isinstance(saved_payload, dict) else None,
        recent_total=len(recent_payload.get("items", [])) if isinstance(recent_payload, dict) else None,
        device_total=len(devices),
    )


async def fetch_all_playlists(client: SpotifyClient) -> dict[str, Any]:
    limit = 50
    offset = 0
    total: int | None = None
    items: list[dict[str, Any]] = []

    while True:
        payload = await client.playlists(limit=limit, offset=offset)
        page_items = payload.get("items", []) if isinstance(payload, dict) else []
        items.extend([item for item in page_items if isinstance(item, dict)])
        if total is None and isinstance(payload, dict) and isinstance(payload.get("total"), int):
            total = payload["total"]
        next_url = payload.get("next") if isinstance(payload, dict) else None
        if not page_items:
            break
        offset += len(page_items)
        if not next_url:
            break
        if total is not None and offset >= total:
            break

    items = sort_playlist_items(items)
    return {
        "items": items,
        "limit": len(items),
        "offset": 0,
        "total": total if total is not None else len(items),
        "next": None,
    }


def sort_playlist_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if settings.spotify_playlist_sort == "alpha":
        return sorted(items, key=lambda item: ((item.get("name") or "Untitled playlist").casefold(), item.get("id") or ""))
    return items


async def build_full_playlists_payload(client: SpotifyClient, request_id: str | None = None) -> dict[str, Any]:
    compact = compact_playlists(await fetch_all_playlists(client))
    payload = {
        "request_id": request_id,
        "kind": "playlists",
        "title": "Playlists",
        "sort_order": settings.spotify_playlist_sort,
        "total": compact.total,
        "items": [library_item_payload(item, slot) for slot, item in enumerate(compact.items)],
    }
    return envelope(version=broker.version, payload=payload, hash_payload={k: v for k, v in payload.items() if k != "request_id"})


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

    if kind == "recent_tracks":
        compact = compact_recent_tracks(await client.recently_played_tracks(limit=50), limit=limit, offset=offset)
        return library_page_payload(
            version=broker.version,
            request_id=request_id,
            page=page,
            kind=kind,
            title="Recent",
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
        return await build_devices_page_payload(client, request_id=request_id, page=page, offset=offset, limit=limit, refresh=True)

    raise ValueError(f"Unsupported library page kind: {kind}")


async def build_devices_payload(
    client: SpotifyClient,
    *,
    request_id: str | None,
    offset: int,
    limit: int,
    refresh: bool = False,
) -> dict[str, Any]:
    devices = await current_devices(client, refresh=refresh)
    return devices_payload(
        version=broker.version,
        request_id=request_id,
        devices=devices,
        active_device_id=active_device_id(devices),
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
    refresh: bool = False,
) -> dict[str, Any]:
    devices = await build_devices_payload(client, request_id=request_id, offset=offset, limit=limit, refresh=refresh)
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

    if request_type == "library_playlists":
        payload = await build_full_playlists_payload(spotify, request_id=request_id)
        await broker.publish_mqtt_retained("library/playlists", payload)
        return {"published_topic": broker.mqtt_topic("library/playlists"), "published_version": payload["version"]}

    if request_type == "devices":
        payload = await build_devices_payload(spotify, request_id=request_id, offset=offset, limit=limit, refresh=True)
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
    request_id = command.get("request_id")
    if not isinstance(request_id, str):
        request_id = None
    command_policy = mqtt_command_policy(command_type)
    await publish_mqtt_status(command_type=command_type, command_request_id=request_id, command_pending=True)
    try:
        if command_type == "play_pause":
            device_id = await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type)
            if broker.current_state and broker.current_state.is_playing:
                await spotify.pause(device_id=device_id)
            else:
                await spotify.play(device_id=device_id)
        elif command_type == "play":
            body = playback_body_from_mqtt(command)
            await spotify.play(body=body, device_id=await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type))
        elif command_type == "pause":
            await spotify.pause(device_id=await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type))
        elif command_type == "next":
            await spotify.next_track(device_id=await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type))
            broker.mark_forward_transition_expected()
        elif command_type == "previous":
            await spotify.previous_track(device_id=await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type))
        elif command_type == "volume_set":
            volume_percent = command.get("volume_percent")
            if not isinstance(volume_percent, int):
                raise ValueError("volume_set requires integer volume_percent.")
            if not 0 <= volume_percent <= 100:
                raise ValueError("volume_percent must be between 0 and 100.")
            if broker.current_state and not broker.current_state.volume_control_supported:
                result = {
                    "ignored": True,
                    "reason": "volume_control_unsupported",
                    "state_version": broker.version,
                    "published_state": False,
                    "state_refresh_ok": None,
                    "state_publish_forced": False,
                    "playback_affecting": command_policy.playback_affecting,
                }
                await publish_mqtt_status(
                    command_type=command_type,
                    command_request_id=request_id,
                    command_pending=False,
                    command_ok=True,
                    command_metadata=result,
                )
                return result
            await spotify.set_volume(volume_percent, await command_device_id(spotify, command.get("device_id")))
        elif command_type == "seek":
            position_ms = command.get("position_ms")
            if not isinstance(position_ms, int) or position_ms < 0:
                raise ValueError("seek requires non-negative integer position_ms.")
            await spotify.seek(
                position_ms,
                await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type),
            )
        elif command_type == "select_source":
            context_uri = command.get("uri") or command.get("context_uri")
            if not isinstance(context_uri, str) or not context_uri:
                raise ValueError("select_source requires uri or context_uri.")
            await spotify.play(
                body={"context_uri": context_uri},
                device_id=await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type),
            )
        elif command_type == "transfer":
            device_id = command.get("device_id")
            if not isinstance(device_id, str) or not device_id:
                raise ValueError("transfer requires device_id.")
            readiness = await target_device_readiness(spotify, TargetDevice(device_id=device_id), refresh=True)
            if not readiness.get("safe_for_live_control", False):
                raise ValueError(f"transfer target is not safe for live control: {','.join(readiness.get('risks', []))}")
            play = command.get("play", True)
            await spotify.transfer_playback(device_id, bool(play))
            if command.get("set_target"):
                store.set_target_device(TargetDevice(device_id=device_id))
        elif command_type == "shuffle_set":
            enabled = command.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("shuffle_set requires boolean enabled.")
            await spotify.set_shuffle(
                enabled,
                await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type),
            )
        elif command_type == "repeat_set":
            mode = command.get("mode")
            if not isinstance(mode, str):
                raise ValueError("repeat_set requires string mode.")
            await spotify.set_repeat(
                mode,
                await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type),
            )
        elif command_type == "save_current_track":
            await spotify.save_track(track_id_from_command_or_state(command, broker.current_state))
        elif command_type == "unsave_current_track":
            await spotify.remove_saved_track(track_id_from_command_or_state(command, broker.current_state))
        elif command_type == "play_library_item":
            body = play_library_item_body(command)
            await spotify.play(body=body, device_id=await verified_live_control_device_id(spotify, command.get("device_id"), command_type=command_type))
        else:
            raise ValueError(f"Unsupported MQTT command type: {command_type}")

        await publish_mqtt_status(
            command_type=command_type,
            command_request_id=request_id,
            command_pending=False,
            command_ok=True,
            command_metadata={"playback_affecting": command_policy.playback_affecting},
        )
        if command_policy.refresh_devices:
            await refresh_devices_after_successful_command(spotify)
        published_state = await refresh_after_successful_command(
            spotify,
            follow_up_delays=settings.command_followup_refresh_delays_for(command_type) if command_policy.follow_up_refresh else (),
        )
        result = {
            "state_version": broker.version,
            "published_state": published_state,
            "state_refresh_ok": published_state,
            "state_publish_forced": True,
            "playback_affecting": command_policy.playback_affecting,
        }
        await publish_mqtt_status(
            command_type=command_type,
            command_request_id=request_id,
            command_pending=False,
            command_ok=True,
            command_metadata=result,
        )
        return result
    except Exception as exc:
        await publish_mqtt_status(
            command_type=command_type,
            command_request_id=request_id,
            command_pending=False,
            command_ok=False,
            command_error=str(exc),
        )
        raise


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
