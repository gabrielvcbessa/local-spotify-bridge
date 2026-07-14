from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.telemetry import BridgeTelemetry, telemetry


def test_telemetry_summarizes_spotify_and_mqtt_by_period_and_type():
    recorder = BridgeTelemetry(max_events=100, retention_seconds=604800)

    recorder.record_spotify_api(
        method="GET",
        path="/me/player",
        status_code=200,
        latency_ms=42.5,
        wait_seconds=0,
        retry_after=None,
        detail='{"is_playing":true}',
    )
    recorder.record_spotify_api(
        method="POST",
        path="/me/player/next",
        status_code=204,
        latency_ms=31,
        wait_seconds=0,
        retry_after=None,
    )
    recorder.record_mqtt_publish(
        topic="rotary/kitchen/state",
        payload_kind="json",
        payload_bytes=128,
        retain=True,
        qos=1,
        published=True,
    )
    recorder.record_mqtt_publish(
        topic="rotary/kitchen/state",
        payload_kind="json",
        payload_bytes=128,
        retain=True,
        qos=1,
        published=False,
        skipped_reason="duplicate_retained_payload",
    )

    snapshot = recorder.snapshot(recent_limit=10)
    period = snapshot["periods"]["1h"]

    assert period["spotify_api_requests"]["total"] == 2
    assert period["spotify_api_requests"]["by_type"]["GET /me/player"]["count"] == 1
    assert period["spotify_api_requests"]["by_type"]["POST /me/player/next"]["last_status_code"] == 204
    assert period["mqtt_postings"]["total"] == 2
    assert period["mqtt_postings"]["by_type"]["rotary/kitchen/state"]["ok"] == 1
    assert period["mqtt_postings"]["by_type"]["rotary/kitchen/state"]["skipped"] == 1

    events = recorder.events(period_label="1h", kind="spotify_api_request", request_type="GET /me/player")
    assert events["total"] == 1
    assert events["items"][0]["detail"] == '{"is_playing":true}'


def test_debug_requests_endpoint_exposes_periods_and_recent_events():
    telemetry.record_spotify_api(
        method="GET",
        path="/unit-test/debug-endpoint",
        status_code=200,
        latency_ms=1,
        wait_seconds=0,
        retry_after=None,
    )

    response = TestClient(app).get("/v1/debug/requests?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert {"1h", "3h", "6h", "1d", "7d"}.issubset(payload["periods"].keys())
    assert payload["periods"]["1h"]["spotify_api_requests"]["by_type"]["GET /unit-test/debug-endpoint"]["count"] >= 1
    assert len(payload["recent"]) <= 5


def test_debug_events_endpoint_filters_by_period_kind_and_type():
    telemetry.record_mqtt_publish(
        topic="rotary/unit-test/state",
        payload_kind="json",
        payload_bytes=18,
        retain=True,
        qos=1,
        published=True,
        detail='{"state":"ok"}',
    )

    response = TestClient(app).get(
        "/v1/debug/events",
        params={
            "period": "1h",
            "kind": "mqtt_posting",
            "request_type": "rotary/unit-test/state",
            "limit": 10,
            "offset": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == "1h"
    assert payload["kind"] == "mqtt_posting"
    assert payload["request_type"] == "rotary/unit-test/state"
    assert payload["total"] >= 1
    assert payload["items"][0]["detail"] == '{"state":"ok"}'


def test_health_exposes_consumer_detection_and_current_polling_thresholds():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["spotify_refresh_token_source"] in {"runtime", "environment", "none"}
    polling = payload["polling"]
    assert polling["mode"] in {"active", "idle"}
    assert isinstance(polling["active_consumers_detected"], bool)
    assert "playback_current_lower_bound_seconds" in polling
    assert "background_current_lower_bound_seconds" in polling
    assert "playlist_current_lower_bound_seconds" in polling
    assert "playback_effective_interval_seconds" in polling
    assert "background_effective_interval_seconds" in polling
    assert "playlist_effective_interval_seconds" in polling
    assert payload["mqtt_protocol"]["schema_version"] == 2
    assert payload["backend_capabilities"]["backend"] == "local_spotify_bridge"
    assert payload["backend_capabilities"]["transport"] == "spotify_web_api"
    assert payload["backend_capabilities"]["library"]["recent_tracks"] is True
    assert payload["backend_capabilities"]["devices"]["readiness"] is True
    assert payload["backend_capabilities"]["art"]["rgb565"] is True
    assert "mqtt_commands" in payload
    assert "consumer_idle_explanation" in payload
    assert "reason" in payload["consumer_idle_explanation"]


def test_health_exposes_cached_target_readiness(monkeypatch):
    previous_cached = main.cached_devices
    main.cached_devices = [{"id": "speaker-1", "name": "Speaker 1", "is_active": False, "supports_volume": False}]
    monkeypatch.setattr(main.store, "get_target_device", lambda: main.TargetDevice(device_id="speaker-1"))
    try:
        response = TestClient(app).get("/health")
    finally:
        main.cached_devices = previous_cached

    assert response.status_code == 200
    readiness = response.json()["target_readiness"]
    assert readiness["resolved_device_id"] == "speaker-1"
    assert readiness["safe_for_live_control"] is True
    assert readiness["ready_for_live_control"] is False
    assert readiness["active"] is False
    assert readiness["volume_control_supported"] is False
    assert readiness["risks"] == ["inactive_device", "volume_unavailable"]


def test_health_exposes_recent_mqtt_command_status(monkeypatch):
    previous_enabled = main.settings.mqtt_enabled
    previous_command = main.broker.last_mqtt_command
    previous_command_at = main.broker.last_mqtt_command_at
    previous_result = main.broker.last_mqtt_command_result
    previous_result_at = main.broker.last_mqtt_command_result_at
    try:
        monkeypatch.setattr(main.settings, "mqtt_enabled", True)
        main.broker.mark_mqtt_command_received({"request_id": "knob-3", "type": "pause"})
        main.broker.mark_mqtt_command_result(
            {
                "ok": True,
                "request_id": "knob-3",
                "command": "pause",
                "state_version": 42,
                "published_state": True,
            }
        )

        response = TestClient(app).get("/health")
    finally:
        monkeypatch.setattr(main.settings, "mqtt_enabled", previous_enabled)
        main.broker.last_mqtt_command = previous_command
        main.broker.last_mqtt_command_at = previous_command_at
        main.broker.last_mqtt_command_result = previous_result
        main.broker.last_mqtt_command_result_at = previous_result_at

    assert response.status_code == 200
    commands = response.json()["mqtt_commands"]
    assert commands["last_command"] == {"type": "pause", "request_id": "knob-3"}
    assert commands["last_command_at"] is not None
    assert commands["last_result"] == {
        "ok": True,
        "command": "pause",
        "request_id": "knob-3",
        "error": None,
        "error_envelope": None,
        "state_version": 42,
        "published_state": True,
        "idempotent_replay": None,
        "received_at": None,
        "completed_at": None,
        "latency_ms": None,
    }
    assert commands["last_result_at"] is not None
    assert commands["cached_result_count"] >= 0
    assert "cached_request_result_count" in commands
    assert "recent" in commands


def test_health_explains_idle_decision_and_retained_payloads(monkeypatch):
    previous_enabled = main.settings.mqtt_enabled
    previous_activity = main.broker.last_mqtt_activity
    previous_activity_at = main.broker.last_mqtt_activity_at
    previous_retained = main.broker._mqtt_retained_payloads.copy()
    try:
        monkeypatch.setattr(main.settings, "mqtt_enabled", True)
        main.broker.last_mqtt_activity = {"source": "availability", "online": False}
        main.broker.last_mqtt_activity_at = "2026-07-13T00:00:00+00:00"
        main.broker._mqtt_retained_payloads.clear()
        main.broker._mqtt_retained_payloads["rotary/kitchen/state"] = {
            "topic": "rotary/kitchen/state",
            "topic_key": "state",
            "payload_kind": "json",
            "payload_bytes": 42,
            "fingerprint": "hash:state",
            "published": True,
            "updated_at": "2026-07-13T00:00:01+00:00",
            "preview": '{"ok":true}',
        }

        response = TestClient(app).get("/health")
    finally:
        monkeypatch.setattr(main.settings, "mqtt_enabled", previous_enabled)
        main.broker.last_mqtt_activity = previous_activity
        main.broker.last_mqtt_activity_at = previous_activity_at
        main.broker._mqtt_retained_payloads.clear()
        main.broker._mqtt_retained_payloads.update(previous_retained)

    assert response.status_code == 200
    payload = response.json()
    assert payload["consumer_idle_explanation"]["reason"] == "mqtt_availability_offline"
    assert payload["consumer_idle_explanation"]["mqtt_offline"] is True
    assert payload["mqtt_retained"][0]["topic_key"] == "state"


def test_debug_dashboard_serves_html():
    response = TestClient(app).get("/debug")

    assert response.status_code == 200
    assert "Local Spotify Bridge Debug" in response.text
    assert "/v1/debug/status" in response.text
    assert "/v1/debug/events" in response.text
    assert "Last MQTT Command" in response.text
    assert "Spotify Connection" in response.text
    assert "Target Readiness" in response.text
    assert "Backend Contract" in response.text
    assert "Consumer Decision" in response.text
    assert "MQTT Command / Request History" in response.text
    assert "Retained MQTT Payloads" in response.text
    assert "spotifyConnectionDetail" in response.text
    assert "spotifyDisconnect" in response.text
    assert "targetReadinessDetail" in response.text
    assert "targetReadinessMeta" in response.text
    assert "backendContractDetail" in response.text
    assert "backendContractMeta" in response.text
    assert "/v1/auth/token" in response.text
    assert "mqttRetainedRows" in response.text
    assert "consumerReasonDetail" in response.text
    assert "mqttHistoryRows" in response.text
    assert "lastCommandDetail" in response.text
    assert "payload-toggle" in response.text
    assert "payload-row" in response.text
    assert "table-layout: fixed" in response.text
