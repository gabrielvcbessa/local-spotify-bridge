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
- MQTT retained playback updates on `local-spotify-bridge/playback` when enabled.
- Active-device capability metadata, including whether Spotify says volume can be controlled.
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
SPOTIFY_REFRESH_TOKEN=...
SPOTIFY_REDIRECT_URI=http://localhost:8090/v1/auth/callback
```

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
6. Spotify redirects back to `/v1/auth/callback`; copy the returned `refresh_token`.
7. Put it in `.env`:

```dotenv
SPOTIFY_REFRESH_TOKEN=...
```

8. Restart the bridge. `/health` should then show `spotify_configured: true`.

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

## REST API

```bash
curl http://localhost:8090/health
curl "http://localhost:8090/v1/state?refresh=true"
curl http://localhost:8090/v1/devices
curl http://localhost:8090/v1/playlists
curl http://localhost:8090/v1/playlists/{playlist_id}/tracks
curl http://localhost:8090/v1/saved-tracks
```

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

MQTT clients subscribe to:

```text
local-spotify-bridge/playback
```

The MQTT message is retained so a knob that wakes up can get the latest state immediately, update
itself, and go back to sleep.
