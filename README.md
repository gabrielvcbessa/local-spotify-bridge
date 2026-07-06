# Local Spotify Bridge

Local Spotify Bridge is a small LAN service for devices that should not talk to Spotify directly.
It keeps Spotify credentials in one plugged-in container, polls and caches playback state, emits
updates only when something meaningful changes, and exposes simple local REST and WebSocket APIs.
It can also publish the latest state to MQTT for Home Assistant, knobs, displays, and other local
listeners.

## What It Provides

- `GET /v1/state` for passive or active clients that want the cached state.
- `GET /v1/state?refresh=true` to actively query Spotify and update the cache.
- `GET /v1/ws` as a WebSocket stream for local listeners.
- MQTT retained knob snapshots/config and inbound knob commands when enabled.
- Active-device capability metadata, including whether Spotify says volume can be controlled.
- A persisted target Spotify device so knob commands can omit device IDs.
- Compact library endpoints shaped for tiny clients.
- Album art proxy/resizer endpoints, including RGB565 output.
- Playback controls: play, pause, next, previous, seek, volume, and output-device transfer.
- Library endpoints for devices, playlists, playlist songs, and saved songs.

## Spotify Setup

Create a Spotify developer app, set a local redirect URI while generating a refresh token, then put
the long-lived credentials in `.env`:

```bash
cp .env.example .env
```

Required variables:

```dotenv
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://localhost:8090/v1/auth/callback
PUBLIC_BASE_URL=http://YOUR_SERVER_IP:8090
```

`SPOTIFY_REFRESH_TOKEN` is optional. The bridge can save it into its runtime store after
`/v1/auth/callback`, and the Docker setup persists that store in the `bridge-data` volume.

The refresh token needs scopes for the bridge features you want:

```text
user-read-playback-state
user-modify-playback-state
playlist-read-private
playlist-read-collaborative
user-library-read
```

### Getting The Refresh Token

1. Create an app at the Spotify Developer Dashboard.
2. Add this redirect URI to the Spotify app settings:

```text
http://localhost:8090/v1/auth/callback
```

3. Start the bridge without `SPOTIFY_REFRESH_TOKEN` set:

```bash
docker compose up --build
```

4. Open this endpoint:

```text
http://localhost:8090/v1/auth/login
```

5. Copy `authorize_url` from the JSON response, open it in your browser, and approve Spotify access.
6. Spotify redirects back to `/v1/auth/callback`; the bridge saves the returned `refresh_token`.
7. `/health` should show `spotify_configured: true` without a restart.

If port `8090` is busy, run with another host port and update the Spotify redirect URI to match:

```bash
PORT=8091 docker compose up --build
```

```dotenv
SPOTIFY_REDIRECT_URI=http://localhost:8091/v1/auth/callback
```

## Run Locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Open:

```text
http://localhost:8090/docs
```

## Run With Docker

```bash
docker compose up --build
```

If your Docker install uses the standalone Compose binary, use `docker-compose up --build`.

With the included test MQTT broker:

```bash
MQTT_ENABLED=true docker compose --profile mqtt up --build
```

Useful MQTT settings:

```dotenv
MQTT_ENABLED=true
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_KNOB_TOPIC_PREFIX=rotary
MQTT_KNOB_DEVICE_ID=kitchen
MQTT_QOS=1
```

## Spotify Rate Limits

The bridge centralizes Spotify Web API traffic and adapts its automatic polling when request volume
gets close to a configurable soft threshold. `POLL_INTERVAL_SECONDS` is always the minimum polling
interval; the bridge will never poll Spotify faster than that value. If Spotify returns `429` with a
`Retry-After` header, future API calls wait for that window and the poller backs off until the retry
window clears.

Useful tuning settings:

```dotenv
POLL_INTERVAL_SECONDS=3
SPOTIFY_RATE_LIMIT_WINDOW_SECONDS=30
SPOTIFY_RATE_LIMIT_SOFT_REQUESTS_PER_WINDOW=20
SPOTIFY_RATE_LIMIT_SOFT_RATIO=0.8
SPOTIFY_RATE_LIMIT_BACKOFF_MULTIPLIER=1.25
SPOTIFY_RATE_LIMIT_MAX_POLL_INTERVAL_SECONDS=60
SPOTIFY_RATE_LIMIT_RETRY_AFTER_PADDING_SECONDS=0.5
```

`/health` includes `rate_limit` diagnostics with the current rolling-window request count, adaptive
poll interval, and any active `Retry-After` wait. Spotify does not publish one universal numeric
quota for every app, so treat `SPOTIFY_RATE_LIMIT_SOFT_REQUESTS_PER_WINDOW` as a conservative local
pressure threshold and tune it if `/health.rate_limit.near_threshold` is frequently true.

## REST API

```bash
curl http://localhost:8090/health
curl "http://localhost:8090/v1/state?refresh=true"
curl http://localhost:8090/v1/devices
curl http://localhost:8090/v1/playlists
curl http://localhost:8090/v1/playlists/{playlist_id}/tracks
curl http://localhost:8090/v1/saved-tracks
curl http://localhost:8090/v1/library/playlists
curl http://localhost:8090/v1/library/playlists/{playlist_id}/tracks
curl http://localhost:8090/v1/library/saved-tracks
curl "http://localhost:8090/v1/knob/snapshot?refresh=true&art_size=180&art_format=rotary-lvgl"
curl "http://localhost:8090/v1/art/current.jpg?size=180"
curl -o current.rgb565 "http://localhost:8090/v1/knob/art/current.rgb565?size=180&format=rotary-lvgl&variant=player-bg"
```

`/v1/playlists`, `/v1/playlists/{id}/tracks`, and `/v1/saved-tracks` remain raw Spotify pass-through
endpoints. Prefer the `/v1/library/...` endpoints for knobs; they return compact items with `id`,
`uri`, `title`, `subtitle`, `image_url`, `duration_ms`, `track_count`, and related small-client fields.

Target device examples:

```bash
curl http://localhost:8090/v1/target
curl -X POST http://localhost:8090/v1/target \
  -H "content-type: application/json" \
  -d '{"device_name":"Living Room Speaker","transfer_playback":true,"play":true}'
```

Once a target is set, control endpoints can omit `device_id`. The bridge resolves the stored target
against Spotify's current device list, so it can recover when Spotify changes device IDs.

Control examples:

```bash
curl -X POST http://localhost:8090/v1/control/pause
curl -X POST http://localhost:8090/v1/control/next
curl -X POST http://localhost:8090/v1/control/play \
  -H "content-type: application/json" \
  -d '{"context_uri":"spotify:playlist:..."}'
curl -X POST http://localhost:8090/v1/control/transfer \
  -H "content-type: application/json" \
  -d '{"device_id":"...","play":true}'
```

After every successful command, the bridge immediately refreshes playback state from Spotify and
publishes changed state through WebSocket and MQTT.

Artwork endpoints:

```text
GET /v1/art/current.jpg?size=180
GET /v1/art/current.rgb565?size=180&swap=lvgl&variant=player-bg
GET /v1/knob/art/current.rgb565?size=180&format=rotary-lvgl&variant=player-bg
GET /v1/knob/art/test-pattern.rgb565?size=180&format=rotary-lvgl
GET /v1/art/proxy.jpg?url={spotify_image_url}&size=180
GET /v1/art/{spotify_image_id}.rgb565?size=180&swap=lvgl&variant=player-bg
```

The JPEG endpoints return resized square JPEGs. The RGB565 endpoints return display-ready square
raw RGB565 bytes after center crop, resize, player-background tuning, and a baked uniform dark
overlay. The generic art endpoints still accept `swap=lvgl` for compatibility; the knob endpoint uses
`format=rotary-lvgl`, meaning the bytes are already in the exact layout Rotary OS writes into
`lv_img_dsc.data`. `swap=none` on generic endpoints returns big-endian RGB565.
`variant=player-bg` applies the final knob player background recipe: no transparent zones, no
gradients, no partial masks, reduced saturation, preserved contrast, and a roughly 45-60% black
overlay baked into the pixels.

The knob-oriented endpoint is:

```text
GET /v1/knob/art/current.rgb565?size=180&format=rotary-lvgl&variant=player-bg
```

Response headers:

```text
Content-Type: application/octet-stream
X-Image-Width: 180
X-Image-Height: 180
X-Image-Format: rgb565
X-Image-Byte-Order: rotary-lvgl
X-Image-Target: rotary-os-lvgl-image-source
X-Image-Variant: player-bg
X-Image-Version: sha256-of-source-art-and-processing-options
X-Image-Hash: sha256-of-final-processed-art-bytes
Cache-Control: public, max-age=86400
```

For `size=180`, the payload is exactly `180 * 180 * 2 = 64800` bytes. Processed artwork is cached
under the bridge data directory by Spotify image id and transform options.

For display diagnostics, request:

```text
GET /v1/knob/art/test-pattern.rgb565?size=180&format=rotary-lvgl
```

The diagnostic payload is red, green, blue, white, and black vertical bars in the same byte order as
normal knob artwork, so firmware can distinguish channel swaps, byte flips, and brightness mistakes
without waiting for album art.

`GET /v1/state` includes these artwork fields when current album art is available:

```json
{
  "album_art_url": "https://i.scdn.co/image/...",
  "album_art_id": "ab67616d0000b273adfc1ac5836f96adac580271",
  "knob_art_url": "http://YOUR_SERVER_IP:8090/v1/knob/art/current.rgb565?size=180&format=rotary-lvgl&variant=player-bg",
  "knob_art_version": "ab67616d0000b273adfc1ac5836f96adac580271"
}
```

The knob should compare `knob_art_version`; if unchanged, it can skip fetching art again.

## Knob Snapshot

The easiest firmware endpoint is:

```text
GET /v1/knob/snapshot?refresh=true&art_size=180&art_format=rotary-lvgl&art_variant=player-bg
```

It returns one compact render payload with deterministic hashes:

```json
{
  "version": 42,
  "payload_hash": "sha256-of-render-relevant-fields",
  "playback_hash": "sha256-of-track-play-state-device-volume-modes",
  "art_hash": "sha256-of-current-knob-art",
  "is_playing": true,
  "progress_ms": 12345,
  "duration_ms": 180000,
  "track": {
    "id": "spotify-track-id",
    "uri": "spotify:track:...",
    "title": "Song name",
    "artists": ["Artist 1", "Artist 2"],
    "artist_text": "Artist 1, Artist 2",
    "album": "Album name"
  },
  "context": {
    "type": "playlist",
    "uri": "spotify:playlist:...",
    "id": "spotify-playlist-id",
    "name": "Playlist name once resolved",
    "display_name": "Playlist name once resolved, otherwise Album name",
    "fallback_name": "Album name"
  },
  "device": {
    "id": "spotify-device-id",
    "name": "Living Room Speaker",
    "type": "Smartphone",
    "is_active": true,
    "is_restricted": null,
    "can_control_playback": true,
    "can_skip_next": true,
    "can_skip_previous": true,
    "volume_percent": 42,
    "volume_control_supported": true
  },
  "modes": {
    "shuffle": false,
    "repeat": "off"
  },
  "art": {
    "id": "spotify-image-id",
    "version": "sha256-of-source-art-and-processing-options",
    "hash": "sha256-of-final-processed-art-bytes",
    "variant": "player-bg",
    "url": "http://YOUR_SERVER_IP:8090/v1/knob/art/current.rgb565?size=180&format=rotary-lvgl&variant=player-bg",
    "width": 180,
    "height": 180,
    "format": "rgb565",
    "byte_order": "rotary-lvgl",
    "content_length": 64800
  },
  "server": {
    "ok": true,
    "spotify_configured": true,
    "updated_at_ms": 1783301820991
  }
}
```

Firmware behavior:

- `payload_hash` changes when anything render-relevant changes.
- `playback_hash` changes when track text, context id/name/display name, play state, device, volume,
  shuffle, or repeat changes.
- `art.version` changes when the source art id or processing recipe changes.
- `art.hash` and top-level `art_hash` are the SHA-256 of the final processed RGB565 bytes.
- If both `art.version` and `art.hash` are unchanged, do not fetch `art.url` again.
- If `device.can_control_playback` is `false`, show state but avoid commands.
- If `device.volume_control_supported` is `false`, do not send volume commands.
- Use `context.display_name` for UI text. The server resolves playlist names when possible and falls
  back to album name for non-playlist or unresolved contexts.

Playlist context names are cached by playlist id for 24 hours. If the name is not cached, the snapshot
returns immediately with `display_name` set to `fallback_name` and triggers a resolve when possible.
Failures are cached briefly for 5 minutes to avoid retry storms; `/v1/knob/snapshot` still succeeds.
Playlist names are not returned by the OAuth callback. The bridge resolves them after auth with the
Spotify Web API, first through `GET /v1/playlists/{playlist_id}` and then, if needed, by scanning the
user playlist library. `/health` includes `playlist_name_cache` so you can see the last playlist id,
whether a name is cached, and whether the latest lookup failed.

## Listener Contract

WebSocket clients connect to:

```text
ws://localhost:8090/v1/ws
```

The bridge immediately sends a snapshot and then sends `playback.changed` messages only when the
track, play state, output device, volume capability, device volume, shuffle/repeat, or progress drift
changes enough to matter.

Knobs should check `state.volume_control_supported` before sending `/v1/control/volume`. If it is
`false`, the active Spotify output device either cannot be volume-controlled through Spotify or did
not report that capability, so the knob should leave volume alone.

MQTT can be used as the lightweight transport for knobs and downstream displays. When enabled, the
bridge publishes the knob snapshot contract to retained state and config topics:

```text
rotary/<device_id>/state          retained, bridge -> knob
rotary/<device_id>/config         retained, bridge -> knob
rotary/<device_id>/command        non-retained, knob -> bridge
rotary/<device_id>/command_result non-retained, bridge -> knob
rotary/<device_id>/availability   retained, knob -> bridge
rotary/<device_id>/library/root   retained, bridge -> knob
rotary/<device_id>/library/page   retained, bridge -> knob
rotary/<device_id>/devices        retained, bridge -> knob
rotary/<device_id>/status         retained, bridge -> knob
rotary/<device_id>/request        non-retained, knob -> bridge
rotary/<device_id>/request_result non-retained, bridge -> knob
```

The default `<device_id>` is `knob`; set `MQTT_KNOB_DEVICE_ID=kitchen` or another stable name for a
specific device. `/health` includes `mqtt_topics` when MQTT is enabled.

The retained `state` message uses the same fields as `/v1/knob/snapshot`, including `payload_hash`,
`playback_hash`, `art_hash`, `context.display_name`, device capability flags, and processed art URL.
The bridge computes the art byte hash from the cached RGB565 payload when possible; if image fetching
fails, state still publishes and the art endpoint remains the source of truth for image headers.

The retained `config` message advertises the active topics, HTTP base URL, art recipe, QoS, and
supported commands/requests. The legacy retained `local-spotify-bridge/playback` envelope is still
published for existing clients.

MQTT command examples:

```json
{ "type": "play_pause" }
{ "type": "next" }
{ "type": "previous" }
{ "type": "volume_set", "volume_percent": 42 }
{ "type": "seek", "position_ms": 30000 }
{ "type": "select_source", "uri": "spotify:playlist:..." }
{ "type": "shuffle_set", "enabled": true }
{ "type": "repeat_set", "mode": "context" }
{ "type": "transfer", "device_id": "...", "play": true, "set_target": true }
{ "type": "play_library_item", "context_uri": "spotify:playlist:...", "item_uri": "spotify:track:..." }
```

After a successful command, the bridge refreshes Spotify state and publishes an updated retained
snapshot. `volume_set` is ignored with a successful command result when the current device reports
`volume_control_supported: false`.

MQTT request examples for non-playback data:

```json
{ "request_id": "knob-1", "type": "library_root" }
{ "request_id": "knob-2", "type": "library_page", "kind": "playlists", "page": 0, "offset": 0, "limit": 3 }
{ "request_id": "knob-3", "type": "library_page", "kind": "playlist_tracks", "parent_uri": "spotify:playlist:...", "offset": 0, "limit": 3 }
{ "request_id": "knob-4", "type": "devices", "offset": 0, "limit": 3 }
{ "request_id": "knob-5", "type": "refresh" }
```

The bridge publishes request payloads to retained library/device topics and answers on
`request_result`. Retained payloads include `version`, `hash`, and `updated_at_ms`, so the knob can
skip redraws when content has not changed.

REST mirrors for debugging:

```text
GET  /v1/knob/status
GET  /v1/knob/library/root
GET  /v1/knob/library/page?kind=playlists&offset=0&limit=3
GET  /v1/knob/library/page?kind=playlist_tracks&parent_uri=spotify:playlist:...&offset=0&limit=3
GET  /v1/knob/devices?offset=0&limit=3
POST /v1/knob/request
POST /v1/knob/command
```

MQTT retained messages do not wake a deeply sleeping Wi-Fi device, but wake-up is fast: the knob
connects, receives retained `config` and `state`, redraws only if hashes changed, and can go back to
sleep after inactivity.
