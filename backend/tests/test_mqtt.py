"""MQTT subscriber tests - decoding, validation and hostile payloads."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import Reading, session_scope
from app.mqtt_client import MqttSubscriber


@pytest.fixture
def subscriber() -> MqttSubscriber:
    return MqttSubscriber()


def _payload(**overrides) -> str:
    base = {
        "temperature_c": 22.0,
        "humidity_pct": 44.0,
        "smoke_ppm": 110.0,
        "air_quality_ppm": 90.0,
        "flame_analog_volts": 0.03,
        "flame_detected": 0,
        "device_status": "SAFE",
    }
    base.update(overrides)
    return json.dumps(base)


def _reading_count() -> int:
    with session_scope() as session:
        return len(session.scalars(select(Reading)).all())


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_valid_telemetry_is_ingested(subscriber):
    assert subscriber.handle_message("fireprotect/esp32-a/telemetry", _payload())
    assert _reading_count() == 1


def test_bytes_payload_accepted(subscriber):
    assert subscriber.handle_message(
        "fireprotect/esp32-a/telemetry", _payload().encode("utf-8")
    )
    assert _reading_count() == 1


def test_device_id_comes_from_topic_not_body(subscriber):
    """A device must not be able to impersonate another via the payload."""
    spoofed = json.loads(_payload())
    spoofed["device_id"] = "victim-device"
    assert subscriber.handle_message(
        "fireprotect/real-device/telemetry", json.dumps(spoofed)
    )
    with session_scope() as session:
        reading = session.scalars(select(Reading)).first()
        assert reading is not None
        assert reading.device_id == "real-device"


def test_stats_track_received_and_rejected(subscriber):
    subscriber.handle_message("fireprotect/esp32-a/telemetry", _payload())
    subscriber.handle_message("fireprotect/esp32-a/telemetry", "not json")
    stats = subscriber.stats
    assert stats["received"] == 2
    assert stats["rejected"] == 1


# --------------------------------------------------------------------------
# Malformed input - none of these may raise
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "{",
        "[1, 2, 3]",
        '"a bare string"',
        "12345",
        "null",
        b"\xff\xfe\x00binary",
    ],
)
def test_malformed_payloads_are_rejected_without_raising(subscriber, raw):
    assert subscriber.handle_message("fireprotect/esp32-a/telemetry", raw) is False
    assert _reading_count() == 0


@pytest.mark.parametrize(
    "topic",
    ["", "fireprotect", "fireprotect/onlytwo", "/", "nonsense"],
)
def test_malformed_topics_are_rejected(subscriber, topic):
    assert subscriber.handle_message(topic, _payload()) is False


def test_non_telemetry_topics_are_ignored(subscriber):
    assert subscriber.handle_message("fireprotect/esp32-a/alert", _payload()) is False
    assert subscriber.handle_message("fireprotect/esp32-a/status", "online") is False
    assert _reading_count() == 0


def test_nan_temperature_is_rejected(subscriber):
    """A failed DHT22 read returns NaN on real hardware."""
    raw = '{"temperature_c": NaN, "humidity_pct": 40, "smoke_ppm": 10, '
    raw += '"air_quality_ppm": 10, "flame_analog_volts": 0.1}'
    assert subscriber.handle_message("fireprotect/esp32-a/telemetry", raw) is False
    assert _reading_count() == 0


def test_infinity_is_rejected(subscriber):
    raw = '{"temperature_c": Infinity, "humidity_pct": 40, "smoke_ppm": 10, '
    raw += '"air_quality_ppm": 10, "flame_analog_volts": 0.1}'
    assert subscriber.handle_message("fireprotect/esp32-a/telemetry", raw) is False


def test_out_of_range_temperature_rejected(subscriber):
    assert (
        subscriber.handle_message(
            "fireprotect/esp32-a/telemetry", _payload(temperature_c=5000.0)
        )
        is False
    )
    assert _reading_count() == 0


def test_missing_required_field_rejected(subscriber):
    assert (
        subscriber.handle_message(
            "fireprotect/esp32-a/telemetry", json.dumps({"temperature_c": 22.0})
        )
        is False
    )


def test_wrong_type_rejected(subscriber):
    assert (
        subscriber.handle_message(
            "fireprotect/esp32-a/telemetry", _payload(temperature_c="hot")
        )
        is False
    )


def test_on_message_never_raises(subscriber):
    """paho's loop thread must never see an exception escape."""

    class Broken:
        topic = "fireprotect/esp32-a/telemetry"

        @property
        def payload(self):
            raise RuntimeError("payload access blew up")

    subscriber._on_message(None, None, Broken())  # must not raise


def test_on_connect_subscribes_to_all_topics(subscriber):
    subscribed: list[str] = []

    class FakeClient:
        def subscribe(self, topic, qos=0):
            subscribed.append(topic)

    subscriber._on_connect(FakeClient(), None, None, 0, None)
    assert subscriber.connected is True
    assert "fireprotect/+/telemetry" in subscribed
    assert "fireprotect/+/alert" in subscribed
    assert "fireprotect/+/status" in subscribed


def test_on_connect_failure_leaves_disconnected(subscriber):
    class FakeClient:
        def subscribe(self, topic, qos=0):
            raise AssertionError("must not subscribe on a refused connection")

    subscriber._on_connect(FakeClient(), None, None, 5, None)
    assert subscriber.connected is False


def test_on_disconnect_clears_connected_flag(subscriber):
    subscriber._connected = True
    subscriber._on_disconnect(None, None, 1)
    assert subscriber.connected is False


def test_start_with_unreachable_broker_returns_false(monkeypatch):
    """An unreachable broker must not prevent the API from booting."""
    sub = MqttSubscriber()
    sub._settings.mqtt_host = "127.0.0.1"
    sub._settings.mqtt_port = 1  # nothing listens on port 1
    try:
        assert sub.start() is False
        assert sub.connected is False
    finally:
        sub.stop()


def test_stop_is_idempotent(subscriber):
    subscriber.stop()
    subscriber.stop()
