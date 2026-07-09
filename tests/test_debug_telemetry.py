from fastapi.testclient import TestClient

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
    polling = response.json()["polling"]
    assert polling["mode"] in {"active", "idle"}
    assert isinstance(polling["active_consumers_detected"], bool)
    assert "playback_current_lower_bound_seconds" in polling
    assert "background_current_lower_bound_seconds" in polling
    assert "playlist_current_lower_bound_seconds" in polling
    assert "playback_effective_interval_seconds" in polling
    assert "background_effective_interval_seconds" in polling
    assert "playlist_effective_interval_seconds" in polling


def test_debug_dashboard_serves_html():
    response = TestClient(app).get("/debug")

    assert response.status_code == 200
    assert "Local Spotify Bridge Debug" in response.text
    assert "/v1/debug/status" in response.text
    assert "/v1/debug/events" in response.text
