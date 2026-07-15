# Agent Guide

This repository is the LAN-side Spotify service for Rotary controllers. It
owns Spotify authentication, playback/library API calls, target-device safety,
state caching, artwork conversion, and the MQTT contract consumed by the M5
StopWatch firmware.

## Runtime Shape

```text
M5 / other clients <-> retained MQTT + request/result topics <-> bridge <-> Spotify Web API
                                      bridge REST/WebSocket APIs <-> local tools
```

Clients must never receive Spotify tokens or secrets. The bridge is the source
of truth after commands: clients may update optimistically for responsiveness,
but follow-up state publications reconcile the UI with Spotify.

## Repository Map

- `app/main.py`: FastAPI app, lifespan tasks, REST/WebSocket endpoints, state
  polling, target readiness, MQTT request/command handlers, follow-up refreshes,
  and the local debug dashboard.
- `app/broker.py`: WebSocket and MQTT connections, topic routing, retained
  publication deduplication, command/result replay, listener activity, and
  in-memory state/device caches.
- `app/mqtt_contract.py`: authoritative protocol name, schema version, feature
  list, topics, commands, requests, config payload, and control-state payload.
- `app/mqtt_commands.py`: command policies and MQTT-to-Spotify body shaping.
- `app/knob_mqtt.py`: compact retained library, device, and status payloads.
- `app/spotify.py`: Spotify HTTP client, OAuth, normalization, scopes, rate
  limits, playback/library calls, and queue preload data.
- `app/models.py`: API request/response models.
- `app/config.py`: environment-backed settings and command refresh profiles.
- `app/store.py`: persisted refresh token and target-device state.
- `app/art.py`: image proxy, resize/cache, and display-ready RGB565 conversion.
- `app/profiles.py`: advertised bridge profile registry.
- `tests/`: unit and contract coverage. MQTT behavior is concentrated in
  `test_mqtt_contract.py`, `test_mqtt_broker.py`, and `test_command_refresh.py`.
- `scripts/verify_live_deployment.py`: checks the deployed build stamp and live
  knob-readiness contract against this checkout.

The matching firmware is a separate repository, normally at
`/Users/gabrielvcbessa/Documents/Rotary Dial`. Its active M5 client lives in
`firmware_m5_stopwatch/`, and its schema fixtures live in
`simulator/fixtures/mqtt/`.

## Local Workflow

Use Python 3.11 or newer. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

When the environment already exists, prefer its executables directly:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Run focused tests while iterating, for example:

```sh
.venv/bin/python -m pytest -q \
  tests/test_mqtt_contract.py tests/test_mqtt_broker.py tests/test_command_refresh.py
```

Local service options:

```sh
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8090
docker compose up --build
MQTT_ENABLED=true docker compose --profile mqtt up --build
```

The test suite mocks external Spotify and MQTT effects. Starting containers,
changing OAuth credentials, sending live commands, or deploying to the LAN
changes external state and should only happen when the task calls for it.

## MQTT Contract Invariants

- `app/mqtt_contract.py` is authoritative. The current protocol is
  `rotary-mqtt-knob`, schema version 2. Coordinate schema/feature changes with
  firmware fixtures and contract tests in the StopWatch repository.
- Retained config/state/control-state/library/device messages are snapshots.
  Command and request results are non-retained RPC responses tied to a unique
  `request_id`; duplicate request IDs replay the prior result.
- Any received availability, command, or request payload is listener activity,
  including malformed command payloads. An explicit retained
  `availability` payload with `online:false` marks the listener offline until
  fresh activity arrives.
- A successful playback, library-play, target/device, or transfer operation
  must publish command status and schedule authoritative state refreshes.
  Queue-changing and device-changing operations also publish the applicable
  queue/device refresh metadata in their command result.
- Target-guarded live controls must pass readiness checks. Do not silently fall
  back to a stale configured Spotify device when active playback proves a
  different usable target.
- Retained-payload deduplication must ignore only fields documented as volatile;
  track, playback, queue, target, capability, and meaningful library/device
  changes must still publish.
- Keep status, health, logs, and error envelopes token-safe. Never expose access
  tokens, refresh tokens, client secrets, MQTT passwords, or authorization
  codes.

## Change Rules

- Add or update focused tests for behavior changes. Run the full test suite and
  Ruff before committing a cross-cutting contract or command-path change.
- Keep Spotify transport details in `app/spotify.py`, MQTT transport/state in
  `app/broker.py`, payload shaping in the MQTT modules, and orchestration/API
  behavior in `app/main.py` unless an existing ownership boundary requires
  otherwise.
- Preserve the bridge's async behavior. Avoid blocking network or image work in
  the event loop.
- Treat `.env`, `data/`, `.venv/`, caches, egg-info, downloaded artwork, and
  generated `app/_build.json` as local/generated state. Do not commit them.
- Preserve unrelated worktree changes and runtime artifacts. Do not clear the
  credential store or target device during ordinary tests.

## Deployment Verification

Images are stamped with the source commit during Docker build. After an
explicit deployment, prove the running server matches this checkout:

```sh
.venv/bin/python scripts/verify_live_deployment.py \
  --base-url http://192.168.68.28:8090
```

Use `/health`, `/v1/target/verify`, and the verifier as evidence; a successful
local test run alone does not prove the LAN container was rebuilt or restarted.
