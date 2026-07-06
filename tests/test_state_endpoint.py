from fastapi.testclient import TestClient

from app.main import app, broker
from app.models import PlaybackSnapshot


def test_state_adds_stable_knob_art_url_and_version():
    previous = broker.current_state
    broker.current_state = PlaybackSnapshot(
        album_art_url="https://i.scdn.co/image/ab67616d0000b273adfc1ac5836f96adac580271",
        album_art_id="ab67616d0000b273adfc1ac5836f96adac580271",
        knob_art_version="ab67616d0000b273adfc1ac5836f96adac580271",
    )
    try:
        response = TestClient(app).get("/v1/state", headers={"host": "bridge.local:8090"})
    finally:
        broker.current_state = previous

    assert response.status_code == 200
    state = response.json()["state"]
    assert state["knob_art_url"] == "http://bridge.local:8090/v1/art/current.rgb565?size=180&swap=lvgl"
    assert state["knob_art_version"] == "ab67616d0000b273adfc1ac5836f96adac580271"
