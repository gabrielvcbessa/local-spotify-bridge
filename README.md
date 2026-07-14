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
7. `/health` should show `spotify_configured: true` and
   `spotify_refresh_token_source: runtime` without a restart.

To disconnect a token paired through the runtime store without editing `.env`:

```bash
curl -X DELETE http://localhost:8090/v1/auth/token
```

This clears the persisted refresh token and cached access token immediately. If
`SPOTIFY_REFRESH_TOKEN` is still set in the environment, the response reports
`env_refresh_token_configured: true` because that token will continue to configure the bridge until
the environment is changed. `/health.spotify_refresh_token_source` reports `runtime`,
`environment`, or `none` so you can tell which token source is currently configuring Spotify.

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
MQTT_KNOB_ART_SIZE=360
COMMAND_FOLLOWUP_REFRESH_PROFILES_SECONDS=play=0.25,0.9;pause=0.25,0.9;play_pause=0.25,0.9;next=0.5,1.5,3.0;previous=0.5,1.5,3.0;select_source=0.7,1.8,3.5;play_library_item=0.7,1.8,3.5;save_current_track=0.4,1.2;unsave_current_track=0.4,1.2;transfer=0.7,1.8,3.5
```

Artwork cache settings:

```dotenv
ART_CACHE_MAX_AGE_SECONDS=604800
ART_CACHE_MAX_BYTES=1073741824
ART_MEMORY_CACHE_MAX_AGE_SECONDS=86400
ART_MEMORY_CACHE_MAX_BYTES=1073741824
```

The disk cache stores final display-ready RGB565 files for up to 7 days by default and prunes
least-recently-used files when it exceeds 1GB. The RAM cache keeps hot RGB565 payloads for up to 1
day by default and also defaults to a 1GB cap. `/health.art_cache` reports the effective limits,
cache path, file count, disk bytes, RAM entries, and RAM bytes.

## Spotify Rate Limits

The bridge centralizes Spotify Web API traffic and adapts its automatic polling when request volume
gets close to a configurable soft threshold. `POLL_INTERVAL_SECONDS` is the minimum playback polling
interval. `SPOTIFY_BACKGROUND_POLL_INTERVAL_SECONDS` is the minimum devices polling interval,
defaulting to 30 seconds while a consumer is active. `SPOTIFY_PLAYLIST_POLL_INTERVAL_SECONDS`
is the automatic playlist/library polling interval and defaults to 2 hours. `SPOTIFY_IDLE_POLL_INTERVAL_SECONDS`
is the shared lower bound for all Spotify polling when no WebSocket client is connected and no recent
MQTT listener activity has arrived, but it never makes a slower poller faster; playlists still use
the playlist interval if it is larger. Listener activity includes retained availability heartbeats,
non-retained commands, and non-retained requests. An `availability` payload with `online:false`
marks the MQTT listener inactive until a fresh command, request, or online heartbeat arrives. Devices
and the full playlist index publish MQTT retained updates only when their semantic payload changes.
Spotify `429 Retry-After` cooldowns are tracked by endpoint group, so a playlist cooldown does not
block `/me/player` now-playing refreshes.

`/health.polling` reports whether the bridge currently detects active consumers and which active or
idle lower bounds are being used. That makes it easy to confirm whether the bridge is in fast
knob-facing mode or quiet idle mode. `/health.consumers.mqtt_last_activity_at` and
`/health.consumers.mqtt_last_activity` show the activity source that is keeping the bridge active.
`/health.mqtt_commands` shows the most recent MQTT command and command result, which is useful when
debugging a button press that reached the bridge but took time to settle through Spotify Connect.

Useful tuning settings:

```dotenv
POLL_INTERVAL_SECONDS=3
SPOTIFY_BACKGROUND_POLL_INTERVAL_SECONDS=30
SPOTIFY_PLAYLIST_POLL_INTERVAL_SECONDS=7200
SPOTIFY_IDLE_POLL_INTERVAL_SECONDS=300
ACTIVE_CONSUMER_TTL_SECONDS=120
DEBUG_TELEMETRY_MAX_EVENTS=50000
DEBUG_TELEMETRY_RETENTION_SECONDS=604800
SPOTIFY_RATE_LIMIT_WINDOW_SECONDS=30
SPOTIFY_RATE_LIMIT_SOFT_REQUESTS_PER_WINDOW=20
SPOTIFY_RATE_LIMIT_SOFT_RATIO=0.8
SPOTIFY_RATE_LIMIT_BACKOFF_MULTIPLIER=1.25
SPOTIFY_RATE_LIMIT_MAX_POLL_INTERVAL_SECONDS=60
SPOTIFY_RATE_LIMIT_RETRY_AFTER_PADDING_SECONDS=0.5
SPOTIFY_PRELOAD_NEXT_ENABLED=true
SPOTIFY_PLAYLIST_SORT=spotify
SPOTIFY_TRACK_END_REFRESH_PADDING_SECONDS=1
COMMAND_FOLLOWUP_REFRESH_DELAYS_SECONDS=0.5,1.5
COMMAND_FOLLOWUP_REFRESH_PROFILES_SECONDS=play=0.25,0.9;pause=0.25,0.9;play_pause=0.25,0.9;next=0.5,1.5,3.0;previous=0.5,1.5,3.0;select_source=0.7,1.8,3.5;play_library_item=0.7,1.8,3.5;save_current_track=0.4,1.2;unsave_current_track=0.4,1.2;transfer=0.7,1.8,3.5
```

`SPOTIFY_PLAYLIST_SORT=spotify` preserves Spotify's playlist order from `/me/playlists`.
`SPOTIFY_PLAYLIST_SORT=alpha` sorts the full retained playlist index alphabetically by title.
While a consumer is active, playback polling also looks at `progress_ms` and `duration_ms`; if the
current track should finish before the next normal poll, the bridge wakes just after the expected
track end. `SPOTIFY_TRACK_END_REFRESH_PADDING_SECONDS` controls that small settle delay. When no
consumer is active, `SPOTIFY_IDLE_POLL_INTERVAL_SECONDS` remains the lower bound.
`COMMAND_FOLLOWUP_REFRESH_PROFILES_SECONDS` overrides the fallback follow-up refresh delays per
command type, which lets quick play/pause updates settle faster than queue-changing or device-changing
commands.

`/health` includes `rate_limit` diagnostics with the current rolling-window request count, adaptive
poll interval, and any active `Retry-After` wait. Spotify does not publish one universal numeric
quota for every app, so treat `SPOTIFY_RATE_LIMIT_SOFT_REQUESTS_PER_WINDOW` as a conservative local
pressure threshold and tune it if `/health.rate_limit.near_threshold` is frequently true.
It also includes the MQTT `protocol` block and backend `capabilities`, so setup and QA tools can
confirm the active backend, transport, library/device/art support, and schema compatibility without
subscribing to retained MQTT config first.

## Debug Dashboard

The bridge includes a local dashboard for request visibility:

```text
GET /debug
GET /v1/debug/status
GET /v1/debug/requests
GET /v1/debug/events
```

`/debug` is a browser page for local operations. It shows the current polling mode, detected
consumers, target readiness, the advertised backend contract, the most recent MQTT command/result,
recent events, and grouped counts for these periods: `1h`, `3h`, `6h`, `12h`, `1d`,
`3d`, and `7d`. Summary rows are clickable: selecting a Spotify request type or MQTT topic opens a
detail view with the latest matching events, status, and a capped response/payload preview. Recent
events are paginated and filtered by the selected period.

The dashboard records two log streams:

- `spotify_api_request`: actual Spotify Web API calls by method and endpoint, including status,
  latency, errors, `Retry-After`, rate-limit wait time, and a capped response preview.
- `mqtt_posting`: MQTT publish attempts by topic, including retained duplicate skips, payload size,
  QoS, retain flag, and a capped payload preview.

Telemetry is in-memory and private to the bridge process. `DEBUG_TELEMETRY_MAX_EVENTS` caps the ring
buffer size and `DEBUG_TELEMETRY_RETENTION_SECONDS` controls how long events stay available. The
defaults keep up to 50,000 events for 7 days.

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
curl http://localhost:8090/v1/knob/library/playlists
curl "http://localhost:8090/v1/knob/snapshot?refresh=true&art_size=360&art_format=rotary-lvgl"
curl "http://localhost:8090/v1/knob/snapshot?refresh=true&art_size=240&art_format=rotary-lvgl"
curl "http://localhost:8090/v1/art/current.jpg?size=180"
curl -o current.rgb565 "http://localhost:8090/v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg"
curl -o current-240.rgb565 "http://localhost:8090/v1/knob/art/current.rgb565?size=240&format=rotary-lvgl&variant=player-bg"
```

`/v1/playlists`, `/v1/playlists/{id}/tracks`, and `/v1/saved-tracks` remain raw Spotify pass-through
endpoints. Prefer the `/v1/library/...` endpoints for knobs; they return compact items with `id`,
`uri`, `title`, `subtitle`, `image_url`, `duration_ms`, `track_count`, and related small-client fields.

Target device examples:

```bash
curl http://localhost:8090/v1/target
curl http://localhost:8090/v1/target/verify
curl -X POST http://localhost:8090/v1/target \
  -H "content-type: application/json" \
  -d '{"device_name":"Living Room Speaker","transfer_playback":true,"play":true}'
```

Once a target is set, control endpoints can omit `device_id`. The bridge resolves the stored target
against Spotify's current device list, so it can recover when Spotify changes device IDs.
`GET /v1/target` also returns a `readiness` block with the resolved device, current risks, volume
support, and whether the target is safe for live control. Requests that transfer playback while
setting a target are refused before calling Spotify when the target cannot be resolved to a real
device ID or Spotify marks it restricted. `GET /v1/target/verify` is the stricter setup/QA gate for
live control proofs: it returns 409 unless the stored target is resolved, unrestricted, active,
volume-controllable, and not at zero volume.

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
publishes changed state through WebSocket and MQTT; it does not wait for the next
`POLL_INTERVAL_SECONDS` tick. Playback-changing commands such as `play`, `pause`, `play_pause`,
`next`, `previous`, `select_source`, `play_library_item`, `save_current_track`,
`unsave_current_track`, and `transfer` also schedule short
follow-up refreshes so Spotify Connect has time to settle before the bridge publishes the final
track/device state. `transfer` refreshes the retained devices topic as well, so a target-device
change is visible without waiting for the background devices poller. Tune those follow-up delays
with `COMMAND_FOLLOWUP_REFRESH_DELAYS_SECONDS`.

For low-power MQTT controllers, keep the listener heartbeat separate from richer telemetry. The M5
StopWatch firmware should publish a minimal retained `availability` heartbeat comfortably inside
`ACTIVE_CONSUMER_TTL_SECONDS` while awake, for example every 30-60 seconds with the default 120-second
TTL. Telemetry can stay slower or idle-aware because any incoming command or request also refreshes
listener activity immediately.

When `SPOTIFY_PRELOAD_NEXT_ENABLED=true`, playback refreshes also make a best-effort request to
Spotify's queue endpoint and expose the first upcoming track as `next_track` in `/v1/state`,
`/v1/knob/snapshot`, WebSocket, and MQTT state payloads. This is advisory preload data: queue,
shuffle, repeat, and Connect device behavior can change before the next command lands, so firmware
should treat `next_track` as optional.

Artwork endpoints:

```text
GET /v1/art/current.jpg?size=180
GET /v1/art/current.rgb565?size=180&swap=lvgl&variant=player-bg
GET /v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg
GET /v1/knob/art/current.rgb565?size=240&format=rotary-lvgl&variant=player-bg
GET /v1/knob/art/test-pattern.rgb565?size=360&format=rotary-lvgl
GET /v1/knob/art/test-pattern.rgb565?size=240&format=rotary-lvgl
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
GET /v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg
```

Response headers:

```text
Content-Type: application/octet-stream
X-Image-Width: 360
X-Image-Height: 360
X-Image-Format: rgb565
X-Image-Byte-Order: rotary-lvgl
X-Image-Target: rotary-os-lvgl-image-source
X-Image-Variant: player-bg
X-Image-Version: sha256-of-source-art-and-processing-options
X-Image-Hash: sha256-of-final-processed-art-bytes
Cache-Control: public, max-age=86400
```

For `size=360`, the payload is exactly `360 * 360 * 2 = 259200` bytes. For `size=240`, the payload
is exactly `240 * 240 * 2 = 115200` bytes. Processed artwork is cached under the bridge data
directory by Spotify image id and transform options, and also held in RAM while hot.

For display diagnostics, request:

```text
GET /v1/knob/art/test-pattern.rgb565?size=360&format=rotary-lvgl
```

The diagnostic payload is red, green, blue, white, and black vertical bars in the same byte order as
normal knob artwork, so firmware can distinguish channel swaps, byte flips, and brightness mistakes
without waiting for album art.

`GET /v1/state` includes these artwork fields when current album art is available:

```json
{
  "album_art_url": "https://i.scdn.co/image/...",
  "album_art_id": "ab67616d0000b273adfc1ac5836f96adac580271",
  "knob_art_url": "http://YOUR_SERVER_IP:8090/v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg",
  "knob_art_version": "ab67616d0000b273adfc1ac5836f96adac580271"
}
```

The knob should compare `knob_art_version`; if unchanged, it can skip fetching art again.

## Knob Snapshot

The easiest firmware endpoint is:

```text
GET /v1/knob/snapshot?refresh=true&art_size=360&art_format=rotary-lvgl&art_variant=player-bg
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
  "next_track": {
    "id": "next-spotify-track-id",
    "uri": "spotify:track:...",
    "title": "Next song name",
    "artists": ["Artist 3"],
    "artist_text": "Artist 3",
    "album": "Next album name",
    "duration_ms": 181000,
    "album_art_id": "next-spotify-image-id",
    "album_art_url": "https://i.scdn.co/image/...",
    "art": {
      "id": "next-spotify-image-id",
      "version": "sha256-of-source-art-and-processing-options",
      "url": "http://bridge.local:8090/v1/art/next-spotify-image-id.rgb565?size=360&swap=lvgl&variant=player-bg",
      "width": 360,
      "height": 360,
      "format": "rgb565",
      "byte_order": "rotary-lvgl",
      "content_length": 259200
    }
  },
  "previous_track": {
    "id": "previous-spotify-track-id",
    "uri": "spotify:track:...",
    "title": "Previous song name",
    "artists": ["Artist 0"],
    "artist_text": "Artist 0",
    "album": "Previous album name",
    "duration_ms": 179000,
    "album_art_id": "previous-spotify-image-id",
    "album_art_url": "https://i.scdn.co/image/...",
    "context_uri": "spotify:playlist:...",
    "album_uri": "spotify:album:...",
    "art": {
      "id": "previous-spotify-image-id",
      "version": "sha256-of-source-art-and-processing-options",
      "url": "http://bridge.local:8090/v1/art/previous-spotify-image-id.rgb565?size=360&swap=lvgl&variant=player-bg",
      "width": 360,
      "height": 360,
      "format": "rgb565",
      "byte_order": "rotary-lvgl",
      "content_length": 259200
    }
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
    "url": "http://YOUR_SERVER_IP:8090/v1/knob/art/current.rgb565?size=360&format=rotary-lvgl&variant=player-bg",
    "width": 360,
    "height": 360,
    "format": "rgb565",
    "byte_order": "rotary-lvgl",
    "content_length": 259200
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
- `playback_hash` changes when track text, optional `next_track` or `previous_track`, context
  id/name/display name, play state, device, volume, shuffle, or repeat changes.
- `art.version` changes when the source art id or processing recipe changes.
- `art.hash` and top-level `art_hash` are the SHA-256 of the final processed RGB565 bytes.
- If both `art.version` and `art.hash` are unchanged, do not fetch `art.url` again.
- If `device.can_control_playback` is `false`, show state but avoid commands.
- If `device.volume_control_supported` is `false`, do not send volume commands.
- `next_track` is optional advisory preload data from Spotify's queue endpoint. It may be `null` and
  should not be treated as a promise that the next command will play that exact track.
- `previous_track` is optional advisory history for knob-side image caching. It is only kept after a
  forward transition caused by `next` or a song reaching the end, and only when the bridge can confirm
  the previous/current tracks are in the same playlist or album and the current track matches the
  previous snapshot's `next_track`. Otherwise it is `null`.
- Use `context.display_name` for UI text. The server resolves playlist names when possible and falls
  back to album name for non-playlist or unresolved contexts.

Playlist context names are cached by playlist id for 24 hours. If the name is not cached, the snapshot
returns immediately with `display_name` set to `fallback_name` and triggers a resolve when possible.
Failures are cached briefly for 5 minutes to avoid retry storms; `/v1/knob/snapshot` still succeeds.
Playlist names are not returned by the OAuth callback. The bridge resolves them after auth with the
Spotify Web API, first through `GET /v1/playlists/{playlist_id}` and then, if needed, by scanning the
user playlist library. Some Spotify-generated radio/personalized playlist ids can return 404 from the
authenticated Web API while still having public metadata on open.spotify.com, so the bridge finally
falls back to Spotify oEmbed title metadata. `/health` includes `playlist_name_cache` so you can see
the last playlist id, whether a name is cached, and whether the latest lookup failed.

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
rotary/<device_id>/control_state  retained, fast controls-only state, bridge -> knob
rotary/<device_id>/config         retained, bridge -> knob
rotary/<device_id>/command        non-retained, knob -> bridge
rotary/<device_id>/command_result non-retained, bridge -> knob
rotary/<device_id>/availability   retained, knob -> bridge
rotary/<device_id>/library/root   retained, bridge -> knob
rotary/<device_id>/library/page   retained, bridge -> knob
rotary/<device_id>/library/playlists retained, full playlist index, bridge -> knob
rotary/<device_id>/devices        retained, bridge -> knob
rotary/<device_id>/status         retained, bridge -> knob
rotary/<device_id>/request        non-retained, knob -> bridge
rotary/<device_id>/request_result non-retained, bridge -> knob
rotary/<device_id>/art/current/rgb565   retained binary RGB565, bridge -> knob
rotary/<device_id>/art/next/rgb565      retained binary RGB565, bridge -> knob
rotary/<device_id>/art/previous/rgb565  retained binary RGB565, bridge -> knob
```

The default `<device_id>` is `knob`; set `MQTT_KNOB_DEVICE_ID=kitchen` or another stable name for a
specific device. `/health` includes `mqtt_topics` when MQTT is enabled.

The retained `control_state` message is a smaller retained payload for fast control surfaces. It
includes play state, track identity/title/artist, progress/duration, device id/name/type/active
state/volume support, shuffle, and repeat. The richer retained `state` message uses the same fields
as `/v1/knob/snapshot`, including `payload_hash`,
`playback_hash`, `art_hash`, `context.display_name`, device capability flags, processed art URL, and
MQTT art metadata. When MQTT is enabled, `art`, `next_track.art`, and `previous_track.art` can include
`mqtt_topic` and `local_cache_path`. Low-power devices can subscribe to `art.mqtt_topic` and receive
the final RGB565 bytes from the retained binary MQTT message instead of doing an HTTP fetch. The
`local_cache_path` is mostly for bridge-local diagnostics or co-located consumers; LAN devices cannot
read that filesystem path directly.

The bridge computes the art byte hash from the cached RGB565 payload when possible; if image fetching
fails, state still publishes and the HTTP art endpoint remains the source of truth for image headers.

The retained `config` message advertises the active topics, HTTP base URL, art recipe, QoS,
supported commands/requests, and backend `capabilities`. The capabilities block is the static
contract that tells constrained clients which backend owns playback, devices, library browsing,
target readiness, and RGB565 art. It also includes an `architecture` block that makes the boundary
explicit: the bridge is the LAN Spotify Web API proxy and OAuth/token owner, MQTT is the recommended
client transport, and direct Spotify on-device is advertised as blocked until browser pairing and
token-storage hardening are solved. The same architecture block advertises
`profile_model=single_bridge_profile` and `multi_profile_selection=false`; multi-profile backend
switching remains blocked until a profile registry exists. Config also includes a `protocol` block:

```json
{
  "name": "rotary-mqtt-knob",
  "schema_version": 2,
  "min_client_schema_version": 2,
  "max_client_schema_version": 2
}
```

Knobs should treat the config as compatible when their local schema version falls inside the
advertised client range. The legacy retained `local-spotify-bridge/playback` envelope is still
published for existing clients.

Retained MQTT publishes are content-deduped per topic. If `state`, `config`, `status`, `devices`,
library payloads, or retained binary art payloads have the same semantic hash as the last payload on
that topic, the bridge skips the publish so sleeping or low-power consumers are not woken for
duplicate data. Volatile fields such as `version`, `updated_at_ms`, and successful `last_poll_at`
updates do not force a publish by themselves. Non-retained `command_result` and `request_result`
messages still publish for each command/request.

The retained `status` payload includes `status` (`ready` or a product setup/degraded state) and
`message` fields that small clients can show directly. It also includes a dynamic `runtime` block
with `configured`, `reachable`, `authenticated`, `target_ready`, `command_pending`, and `degraded`
flags, plus cached `target_readiness` when the bridge has a recent devices list. That lets clients
distinguish unresolved, restricted, inactive, zero-volume, and no-volume targets without making
another request. `target_readiness.safe_for_live_control` stays permissive enough for device transfer
attempts, while `target_readiness.ready_for_live_control` is the stricter preflight gate for physical
playback/volume tests: the target must be resolved, unrestricted, active, volume-controllable, and
not sitting at zero volume. The same block includes `active`, `volume_control_supported`,
`muted_or_zero_volume`, and `last_update_at` fields for setup and QA surfaces.
`GET /v1/target/verify` uses the same strict readiness rule and returns 409 with the readiness block
when setup should refuse a live command proof.
Successful REST control and target-device changes also stamp a `last_command` pulse into `status`,
forcing a retained status update even if Spotify's playback state has not settled into a new
snapshot yet. Successful pulses include `ok:true`; failed MQTT command pulses include `ok:false` and
an `error` string when the bridge has a concrete failure reason. MQTT commands include the original
`request_id` in that pulse when one was supplied, so clients can use retained status as a backup
acknowledgement if they miss the non-retained `command_result` and `runtime.command_pending` is
false.

MQTT command examples:

```json
{ "request_id": "knob-101", "type": "play_pause" }
{ "request_id": "knob-102", "type": "next" }
{ "request_id": "knob-103", "type": "previous" }
{ "request_id": "knob-104", "type": "volume_set", "volume_percent": 42 }
{ "request_id": "knob-105", "type": "seek", "position_ms": 30000 }
{ "request_id": "knob-106", "type": "select_source", "uri": "spotify:playlist:..." }
{ "request_id": "knob-107", "type": "shuffle_set", "enabled": true }
{ "request_id": "knob-108", "type": "repeat_set", "mode": "context" }
{ "request_id": "knob-109", "type": "transfer", "device_id": "...", "play": true, "set_target": true }
{ "request_id": "knob-110", "type": "play_library_item", "context_uri": "spotify:playlist:...", "item_uri": "spotify:track:..." }
{ "request_id": "knob-111", "type": "save_current_track", "track_uri": "spotify:track:..." }
{ "request_id": "knob-112", "type": "unsave_current_track", "track_uri": "spotify:track:..." }
```

After a successful command, the bridge refreshes Spotify state and publishes an updated retained
snapshot. `volume_set` is ignored with a successful command result when the current device reports
`volume_control_supported: false`.
Commands should include a stable `request_id`. If the same `request_id` is received again, the bridge
replays the cached `command_result` instead of sending the command to Spotify twice. Command results
include `received_at`, `completed_at`, `latency_ms`, and `idempotent_replay` when a cached result was
replayed.

MQTT request examples for non-playback data:

```json
{ "request_id": "knob-1", "type": "library_root" }
{ "request_id": "knob-1b", "type": "library_playlists" }
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
GET  /v1/knob/library/playlists
GET  /v1/knob/library/page?kind=playlists&offset=0&limit=3
GET  /v1/knob/library/page?kind=playlist_tracks&parent_uri=spotify:playlist:...&offset=0&limit=3
GET  /v1/knob/devices?offset=0&limit=3
POST /v1/knob/request
POST /v1/knob/command
```

MQTT retained messages do not wake a deeply sleeping Wi-Fi device, but wake-up is fast: the knob
connects, receives retained `config` and `state`, redraws only if hashes changed, and can go back to
sleep after inactivity.
