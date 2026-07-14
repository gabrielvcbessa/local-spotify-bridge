import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.broker import ConnectionBroker, mqtt_payload_fingerprint
from app.config import Settings
from app.knob_mqtt import status_payload
from app.models import PlaybackSnapshot
from app.store import TargetDevice


class FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False):
        self.published.append((topic, payload, qos, retain))


@pytest.mark.anyio
async def test_mqtt_publish_includes_legacy_and_retained_knob_snapshot():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_TOPIC_PREFIX="local-spotify-bridge",
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    async def snapshot_factory(version, state):
        return {
            "version": version,
            "payload_hash": "payload",
            "playback_hash": "playback",
            "art_hash": "art",
            "state_title": state.title if state else None,
        }

    broker.set_mqtt_snapshot_factory(snapshot_factory)

    await broker.publish("playback.changed", PlaybackSnapshot(title="Song"))

    assert mqtt.published[0][0] == "local-spotify-bridge/playback"
    assert mqtt.published[0][2:] == (1, True)
    assert mqtt.published[1][0] == "rotary/kitchen/state"
    assert mqtt.published[1][2:] == (1, True)
    assert json.loads(mqtt.published[1][1])["state_title"] == "Song"


@pytest.mark.anyio
async def test_mqtt_config_is_retained():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt
    broker.set_mqtt_config_factory(lambda: {"topics": broker.mqtt_topics()})

    await broker.publish_mqtt_config()

    assert mqtt.published == [
        (
            "rotary/kitchen/config",
            json.dumps({"topics": broker.mqtt_topics()}),
            1,
            True,
        )
    ]


@pytest.mark.anyio
async def test_mqtt_config_skips_duplicate_payloads():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt
    broker.set_mqtt_config_factory(lambda: {"topics": broker.mqtt_topics()})

    await broker.publish_mqtt_config()
    await broker.publish_mqtt_config()

    assert len(mqtt.published) == 1


@pytest.mark.anyio
async def test_mqtt_retained_publish_skips_duplicate_payloads_by_topic():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    await broker.publish_mqtt_retained("status", {"hash": "same", "updated_at_ms": 1})
    await broker.publish_mqtt_retained("status", {"hash": "same", "updated_at_ms": 2})
    await broker.publish_mqtt_retained("devices", {"hash": "same", "updated_at_ms": 2})
    await broker.publish_mqtt_retained("status", {"hash": "different", "updated_at_ms": 3})

    assert [entry[0] for entry in mqtt.published] == [
        "rotary/kitchen/status",
        "rotary/kitchen/devices",
        "rotary/kitchen/status",
    ]
    retained = broker.retained_payload_status()
    retained_by_key = {entry["topic_key"]: entry for entry in retained}
    assert retained_by_key["status"]["published"] is True
    assert retained_by_key["status"]["payload_bytes"] > 0
    assert retained_by_key["status"]["preview"].startswith("{")
    assert retained_by_key["devices"]["published"] is True


@pytest.mark.anyio
async def test_mqtt_retained_status_records_duplicate_skips():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    await broker.publish_mqtt_retained("status", {"hash": "same", "updated_at_ms": 1})
    await broker.publish_mqtt_retained("status", {"hash": "same", "updated_at_ms": 2})

    retained = broker.retained_payload_status()
    assert retained[0]["topic"] == "rotary/kitchen/status"
    assert retained[0]["topic_key"] == "status"
    assert retained[0]["published"] is False
    assert retained[0]["fingerprint"].startswith("hash:")


@pytest.mark.anyio
async def test_mqtt_retained_binary_publish_skips_duplicate_payloads_by_topic():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    await broker.publish_mqtt_retained_bytes("art/current/rgb565", b"1234")
    await broker.publish_mqtt_retained_bytes("art/current/rgb565", b"1234")
    await broker.publish_mqtt_retained_bytes("art/current/rgb565", b"5678")

    assert mqtt.published == [
        ("rotary/kitchen/art/current/rgb565", b"1234", 1, True),
        ("rotary/kitchen/art/current/rgb565", b"5678", 1, True),
    ]


def test_mqtt_payload_fingerprint_ignores_volatile_fields_without_hashes():
    first = {
        "version": 1,
        "updated_at_ms": 100,
        "server": {"updated_at_ms": 100},
        "value": "same",
    }
    second = {
        "version": 2,
        "updated_at_ms": 200,
        "server": {"updated_at_ms": 200},
        "value": "same",
    }

    assert mqtt_payload_fingerprint(first) == mqtt_payload_fingerprint(second)


def test_mqtt_payload_fingerprint_ignores_progress_only_state_changes():
    first = {
        "version": 1,
        "payload_hash": "progress-hash-1",
        "playback_hash": "playback-hash-1",
        "progress_ms": 10_000,
        "is_playing": True,
        "track": {"id": "track-1", "title": "Song"},
        "server": {"updated_at_ms": 100},
    }
    second = {
        "version": 2,
        "payload_hash": "progress-hash-2",
        "playback_hash": "playback-hash-2",
        "progress_ms": 16_000,
        "is_playing": True,
        "track": {"id": "track-1", "title": "Song"},
        "server": {"updated_at_ms": 200},
    }

    assert mqtt_payload_fingerprint(first) == mqtt_payload_fingerprint(second)


def test_mqtt_payload_fingerprint_keeps_track_changes_meaningful():
    first = {
        "payload_hash": "hash-1",
        "playback_hash": "playback-1",
        "progress_ms": 10_000,
        "track": {"id": "track-1", "title": "Song"},
    }
    second = {
        "payload_hash": "hash-2",
        "playback_hash": "playback-2",
        "progress_ms": 0,
        "track": {"id": "track-2", "title": "Next song"},
    }

    assert mqtt_payload_fingerprint(first) != mqtt_payload_fingerprint(second)


def test_mqtt_status_hash_ignores_successful_poll_timestamp():
    first = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=None,
        target=None,
        mqtt_connected=True,
    )
    second = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:01:00+00:00",
        last_error=None,
        current_state=None,
        target=None,
        mqtt_connected=True,
    )

    assert first["last_poll_at"] != second["last_poll_at"]
    assert first["hash"] == second["hash"]


def test_mqtt_status_payload_exposes_m5_status_fields_and_command_pulses():
    ready = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=PlaybackSnapshot(device_id="speaker-1"),
        target=None,
        mqtt_connected=True,
        command_pulse={"type": "play", "request_id": "knob-play-1", "completed_at": "2026-07-06T10:00:01+00:00"},
    )
    degraded = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error="Spotify offline",
        current_state=None,
        target=None,
        mqtt_connected=True,
    )
    next_pulse = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=PlaybackSnapshot(device_id="speaker-1"),
        target=None,
        mqtt_connected=True,
        command_pulse={"type": "next", "completed_at": "2026-07-06T10:00:02+00:00"},
    )

    assert ready["status"] == "ready"
    assert ready["message"] == "Ready"
    assert ready["runtime"]["backend"] == "local_spotify_bridge"
    assert ready["runtime"]["transport"] == "spotify_web_api"
    assert ready["runtime"]["configured"] is True
    assert ready["runtime"]["reachable"] is True
    assert ready["runtime"]["authenticated"] is True
    assert ready["runtime"]["command_pending"] is False
    assert ready["last_command"]["type"] == "play"
    assert ready["last_command"]["request_id"] == "knob-play-1"
    assert degraded["status"] == "backend_unreachable"
    assert degraded["message"] == "Spotify offline"
    assert degraded["runtime"]["degraded"] is True
    assert degraded["runtime"]["state"] == "backend_unreachable"
    assert mqtt_payload_fingerprint(ready) != mqtt_payload_fingerprint(next_pulse)


def test_mqtt_status_payload_exposes_pending_command_runtime_state():
    payload = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=PlaybackSnapshot(device_id="speaker-1"),
        target=None,
        mqtt_connected=True,
        command_pending=True,
    )

    assert payload["runtime"]["command_pending"] is True
    assert payload["runtime"]["configured"] is True
    assert payload["runtime"]["reachable"] is True


def test_mqtt_status_payload_exposes_product_setup_states():
    unconfigured = status_payload(
        version=1,
        spotify_configured=False,
        last_poll_at=None,
        last_error=None,
        current_state=None,
        target=None,
        mqtt_connected=True,
    )
    no_playback = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=None,
        target=None,
        mqtt_connected=True,
    )
    auth_expired = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error="Spotify returned 401 unauthorized",
        current_state=None,
        target=None,
        mqtt_connected=True,
    )
    target_not_ready = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=PlaybackSnapshot(device_id="speaker-1"),
        target=TargetDevice(device_id="speaker-2"),
        mqtt_connected=True,
        target_readiness={"safe_for_live_control": False, "risks": ["target_not_found"]},
    )

    assert unconfigured["status"] == "spotify_not_configured"
    assert unconfigured["ok"] is False
    assert unconfigured["spotify_reachable"] is False
    assert unconfigured["runtime"]["configured"] is False
    assert unconfigured["runtime"]["target_ready"] is False
    assert no_playback["status"] == "no_active_playback"
    assert auth_expired["status"] == "auth_expired"
    assert auth_expired["runtime"]["authenticated"] is False
    assert target_not_ready["status"] == "target_not_ready"


def test_mqtt_status_payload_includes_target_readiness():
    readiness = {
        "safe_for_live_control": False,
        "risks": ["target_not_found"],
        "resolved_device_id": None,
    }
    payload = status_payload(
        version=1,
        spotify_configured=True,
        last_poll_at="2026-07-06T10:00:00+00:00",
        last_error=None,
        current_state=None,
        target=None,
        mqtt_connected=True,
        target_readiness=readiness,
    )

    assert payload["target_readiness"] == readiness
    assert mqtt_payload_fingerprint(payload).startswith("hash:")


@pytest.mark.anyio
async def test_mqtt_command_publishes_non_retained_result():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    async def command_handler(command):
        return {"seen": command["type"]}

    broker.set_mqtt_command_handler(command_handler)

    await broker._handle_mqtt_message("rotary/kitchen/command", '{"type":"next"}')

    assert mqtt.published[0][0] == "rotary/kitchen/command_result"
    assert mqtt.published[0][2:] == (1, False)
    result = json.loads(mqtt.published[0][1])
    assert {key: value for key, value in result.items() if key not in {"received_at", "completed_at", "latency_ms"}} == {
        "ok": True,
        "request_id": None,
        "command": "next",
        "seen": "next",
    }
    assert result["received_at"] is not None
    assert result["completed_at"] is not None
    assert result["latency_ms"] >= 0
    assert broker.mqtt_command_status()["last_command"] == {"type": "next", "request_id": None}
    assert broker.mqtt_command_status()["last_command_at"] is not None
    assert broker.mqtt_command_status()["last_result"] == {
        "ok": True,
        "command": "next",
        "request_id": None,
        "error": None,
        "error_envelope": None,
        "state_version": None,
        "published_state": None,
        "idempotent_replay": None,
        "received_at": result["received_at"],
        "completed_at": result["completed_at"],
        "latency_ms": result["latency_ms"],
    }
    assert broker.mqtt_command_status()["last_result_at"] is not None


@pytest.mark.anyio
async def test_mqtt_command_status_tracks_pending_command():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt
    started = asyncio.Event()
    release = asyncio.Event()

    async def command_handler(command):
        started.set()
        await release.wait()
        return {"seen": command["type"]}

    broker.set_mqtt_command_handler(command_handler)
    task = asyncio.create_task(
        broker._handle_mqtt_message("rotary/kitchen/command", '{"request_id":"knob-pending","type":"next"}')
    )
    await started.wait()

    status = broker.mqtt_command_status()
    assert status["pending_command"] == {"type": "next", "request_id": "knob-pending"}
    assert status["pending_command_at"] is not None
    assert status["pending_command_count"] == 1

    release.set()
    await task

    status = broker.mqtt_command_status()
    assert status["pending_command"] is None
    assert status["pending_command_at"] is None
    assert status["pending_command_count"] == 1


@pytest.mark.anyio
async def test_mqtt_command_status_keeps_failed_command_context():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    async def command_handler(_command):
        raise ValueError("spotify lagged")

    broker.set_mqtt_command_handler(command_handler)

    await broker._handle_mqtt_message(
        "rotary/kitchen/command",
        '{"request_id":"knob-9","type":"pause"}',
    )

    result = json.loads(mqtt.published[0][1])
    assert {key: value for key, value in result.items() if key not in {"received_at", "completed_at", "latency_ms"}} == {
        "ok": False,
        "error": "spotify lagged",
        "error_envelope": {
            "code": "invalid_payload",
            "type": "ValueError",
            "message": "spotify lagged",
            "source": "mqtt_command",
        },
        "request_id": "knob-9",
        "command": "pause",
    }
    assert broker.mqtt_command_status()["last_command"] == {"type": "pause", "request_id": "knob-9"}
    assert broker.mqtt_command_status()["last_result"] == {
        "ok": False,
        "command": "pause",
        "request_id": "knob-9",
        "error": "spotify lagged",
        "error_envelope": {
            "code": "invalid_payload",
            "type": "ValueError",
            "message": "spotify lagged",
            "source": "mqtt_command",
        },
        "state_version": None,
        "published_state": None,
        "idempotent_replay": None,
        "received_at": result["received_at"],
        "completed_at": result["completed_at"],
        "latency_ms": result["latency_ms"],
    }


@pytest.mark.anyio
async def test_mqtt_command_invalid_json_returns_error_envelope():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    async def command_handler(_command):
        raise AssertionError("handler should not be called")

    broker.set_mqtt_command_handler(command_handler)

    await broker._handle_mqtt_message("rotary/kitchen/command", "{")

    result = json.loads(mqtt.published[0][1])
    assert result["ok"] is False
    assert result["error_envelope"]["code"] == "invalid_payload"
    assert result["error_envelope"]["type"] == "JSONDecodeError"
    assert result["error_envelope"]["source"] == "mqtt_command"


@pytest.mark.anyio
async def test_mqtt_command_replays_duplicate_request_id_without_rehandling():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt
    handled = 0

    async def command_handler(command):
        nonlocal handled
        handled += 1
        return {"handled": handled, "seen": command["type"]}

    broker.set_mqtt_command_handler(command_handler)

    await broker._handle_mqtt_message("rotary/kitchen/command", '{"request_id":"knob-1","type":"next"}')
    await broker._handle_mqtt_message("rotary/kitchen/command", '{"request_id":"knob-1","type":"next"}')

    first = json.loads(mqtt.published[0][1])
    second = json.loads(mqtt.published[1][1])
    assert handled == 1
    assert first["handled"] == 1
    assert first.get("idempotent_replay") is None
    assert second["handled"] == 1
    assert second["idempotent_replay"] is True
    assert broker.mqtt_command_status()["cached_result_count"] == 1


@pytest.mark.anyio
async def test_mqtt_request_publishes_request_result():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
            MQTT_QOS=1,
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    async def request_handler(request):
        return {"published_topic": "rotary/kitchen/library/root", "seen": request["type"]}

    broker.set_mqtt_request_handler(request_handler)

    await broker._handle_mqtt_message(
        "rotary/kitchen/request",
        '{"request_id":"knob-1","type":"library_root"}',
    )

    assert mqtt.published[0][0] == "rotary/kitchen/request_result"
    assert mqtt.published[0][2:] == (1, False)
    result = json.loads(mqtt.published[0][1])
    assert {key: value for key, value in result.items() if key not in {"received_at", "completed_at", "latency_ms"}} == {
        "ok": True,
        "request_id": "knob-1",
        "request": "library_root",
        "published_topic": "rotary/kitchen/library/root",
        "seen": "library_root",
    }
    assert result["received_at"] is not None
    assert result["completed_at"] is not None
    assert result["latency_ms"] >= 0
    status = broker.mqtt_command_status()
    assert status["cached_request_result_count"] == 1
    assert status["recent"][0]["label"] == "request"
    assert status["recent"][0]["request_id"] == "knob-1"


@pytest.mark.anyio
async def test_mqtt_request_replays_duplicate_request_id_without_rehandling():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt
    handled = 0

    async def request_handler(request):
        nonlocal handled
        handled += 1
        return {"handled": handled, "seen": request["type"]}

    broker.set_mqtt_request_handler(request_handler)

    await broker._handle_mqtt_message("rotary/kitchen/request", '{"request_id":"knob-r1","type":"devices"}')
    await broker._handle_mqtt_message("rotary/kitchen/request", '{"request_id":"knob-r1","type":"devices"}')

    first = json.loads(mqtt.published[0][1])
    second = json.loads(mqtt.published[1][1])
    assert handled == 1
    assert first["handled"] == 1
    assert first.get("idempotent_replay") is None
    assert second["handled"] == 1
    assert second["idempotent_replay"] is True
    assert broker.mqtt_command_status()["cached_request_result_count"] == 1


@pytest.mark.anyio
async def test_mqtt_topics_include_planning_doc_topics():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
        )
    )

    assert broker.mqtt_topics() | {} == {
        "legacy_playback": "local-spotify-bridge/playback",
        "state": "rotary/kitchen/state",
        "control_state": "rotary/kitchen/control_state",
        "config": "rotary/kitchen/config",
        "command": "rotary/kitchen/command",
        "command_result": "rotary/kitchen/command_result",
        "availability": "rotary/kitchen/availability",
        "library_root": "rotary/kitchen/library/root",
        "library_page": "rotary/kitchen/library/page",
        "library_playlists": "rotary/kitchen/library/playlists",
        "devices": "rotary/kitchen/devices",
        "status": "rotary/kitchen/status",
        "request": "rotary/kitchen/request",
        "request_result": "rotary/kitchen/request_result",
        "art_current": "rotary/kitchen/art/current/rgb565",
        "art_next": "rotary/kitchen/art/next/rgb565",
        "art_previous": "rotary/kitchen/art/previous/rgb565",
    }


@pytest.mark.anyio
async def test_mqtt_availability_is_recorded_without_command_result():
    broker = ConnectionBroker(
        Settings(
            MQTT_ENABLED=True,
            MQTT_KNOB_TOPIC_PREFIX="rotary",
            MQTT_KNOB_DEVICE_ID="kitchen",
        )
    )
    mqtt = FakeMqttClient()
    broker._mqtt_client = mqtt

    await broker._handle_mqtt_message("rotary/kitchen/availability", '{"online":true}')

    assert broker.last_mqtt_availability == {"online": True}
    assert broker.last_mqtt_availability_at is not None
    assert broker.last_mqtt_activity == {"source": "availability", "online": True}
    assert broker.last_mqtt_activity_at is not None
    assert mqtt.published == []


@pytest.mark.asyncio
async def test_mqtt_command_and_request_refresh_consumer_activity():
    broker = ConnectionBroker(Settings(MQTT_ENABLED=True, MQTT_KNOB_TOPIC_PREFIX="rotary", MQTT_KNOB_DEVICE_ID="kitchen"))
    broker._mqtt_client = FakeMqttClient()

    async def command_handler(command):
        return {"command_type": command["type"]}

    async def request_handler(request):
        return {"request_type": request["type"]}

    broker.set_mqtt_command_handler(command_handler)
    broker.set_mqtt_request_handler(request_handler)

    await broker._handle_mqtt_message("rotary/kitchen/command", "{\"type\":\"next\"}")

    assert broker.last_mqtt_activity == {"source": "command"}
    assert broker.has_active_consumers(ttl_seconds=120)

    await broker._handle_mqtt_message("rotary/kitchen/request", "{\"type\":\"refresh\"}")

    assert broker.last_mqtt_activity == {"source": "request"}
    assert broker.has_active_consumers(ttl_seconds=120)


def test_broker_active_consumers_uses_recent_mqtt_activity():
    broker = ConnectionBroker(Settings())
    broker.mark_mqtt_activity(source="availability", payload={"online": True})

    assert broker.has_active_consumers(ttl_seconds=120)
    assert broker.consumer_status(ttl_seconds=120)["mqtt_active"] is True


def test_broker_active_consumers_ignores_stale_or_offline_mqtt_activity():
    broker = ConnectionBroker(Settings())
    broker.last_mqtt_activity = {"source": "availability", "online": True}
    broker.last_mqtt_activity_at = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()

    assert not broker.has_active_consumers(ttl_seconds=120)

    broker.mark_mqtt_activity(source="availability", payload={"online": False})

    assert not broker.has_active_consumers(ttl_seconds=120)
