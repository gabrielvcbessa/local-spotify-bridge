import json

import pytest

from app.broker import ConnectionBroker
from app.config import Settings
from app.models import PlaybackSnapshot


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
    assert json.loads(mqtt.published[0][1]) == {
        "ok": True,
        "command": "next",
        "result": {"seen": "next"},
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
    assert mqtt.published == []
