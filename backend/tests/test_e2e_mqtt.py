"""End-to-end pipeline test over a real MQTT broker.

Every other test exercises a slice. This one runs the whole chain the way the
system actually runs:

    simulator -> real MQTT broker -> paho subscriber -> ingest -> RF inference
              -> SQLite -> WebSocket broadcast

An in-process ``amqtt`` broker stands in for Mosquitto. It speaks real MQTT
over a real socket, so the paho client, topic wildcards, QoS and JSON framing
are all genuinely exercised - only the broker *binary* differs from production.

Skipped (not silently passed) if amqtt is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Alert, Device, Reading, session_scope
from app.ingest import IngestService, get_ingest_service
from app.mqtt_client import MqttSubscriber

amqtt_broker = pytest.importorskip(
    "amqtt.broker", reason="amqtt not installed; cannot run the real-broker E2E test"
)

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "simulator"
sys.path.insert(0, str(SIMULATOR_DIR))

from virtual_device import VirtualDevice  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BrokerThread:
    """Runs an amqtt broker on its own event loop in a background thread."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broker = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            config = {
                "listeners": {
                    "default": {
                        "type": "tcp",
                        "bind": f"127.0.0.1:{self.port}",
                        "max_connections": 20,
                    }
                },
                # Anonymous auth must be enabled explicitly. With no auth
                # plugin configured amqtt rejects every CONNECT during session
                # setup ("Failed to initialize client session: No more data"),
                # which looks like a protocol error but is an ACL denial.
                "plugins": {
                    "amqtt.plugins.authentication.AnonymousAuthPlugin": {
                        "allow_anonymous": True
                    }
                },
            }

            async def boot() -> None:
                # Broker.__init__ calls asyncio.get_running_loop(), so it must
                # be constructed inside the loop, not before run_until_complete.
                self._broker = amqtt_broker.Broker(config)
                await self._broker.start()
                self._ready.set()

            loop.run_until_complete(boot())
            loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True, name="amqtt-broker")
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("amqtt broker failed to start")
        # Confirm the port really accepts connections before returning.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("amqtt broker never accepted a connection")

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._broker is not None:
            future = asyncio.run_coroutine_threadsafe(self._broker.shutdown(), loop)
            # Best-effort shutdown: a broker that is already gone must not fail
            # teardown and mask the actual test result.
            with contextlib.suppress(Exception):
                future.result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)


@pytest.fixture
def broker():
    instance = BrokerThread(_free_port())
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def subscriber(broker):
    """A backend MQTT subscriber connected to the live broker."""
    settings = Settings(mqtt_host="127.0.0.1", mqtt_port=broker.port)
    sub = MqttSubscriber(settings)
    assert sub.start(), "backend failed to connect to the test broker"

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not sub.connected:
        time.sleep(0.05)
    assert sub.connected, "backend MQTT client never reported connected"
    # Give the SUBSCRIBE a moment to be acknowledged before publishing.
    time.sleep(0.5)
    try:
        yield sub
    finally:
        sub.stop()


def _publisher(port: int):
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:  # pragma: no cover - paho < 2.0
        client = mqtt.Client()
    client.connect("127.0.0.1", port, 60)
    client.loop_start()
    return client


def _wait_for(predicate, timeout: float = 25.0, interval: float = 0.15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _count(model) -> int:
    with session_scope() as session:
        return len(session.scalars(select(model)).all())


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_single_telemetry_message_reaches_the_database(broker, subscriber):
    client = _publisher(broker.port)
    try:
        payload = {
            "temperature_c": 23.1,
            "humidity_pct": 44.0,
            "smoke_ppm": 570.0,
            "air_quality_ppm": 118.0,
            "flame_analog_volts": 0.15,
            "flame_detected": 0,
            "device_status": "SAFE",
            "location": "E2E Lab",
        }
        client.publish("fireprotect/e2e-01/telemetry", json.dumps(payload), qos=1)

        assert _wait_for(lambda: _count(Reading) >= 1), "reading never reached the DB"
    finally:
        client.loop_stop()
        client.disconnect()

    with session_scope() as session:
        reading = session.scalars(select(Reading)).first()
        assert reading is not None
        assert reading.device_id == "e2e-01"
        assert reading.temperature_c == pytest.approx(23.1)
        # Proof the server-side model actually ran on the way through.
        assert reading.server_status in {"SAFE", "WARNING", "FIRE"}
        assert 0.0 <= reading.fire_probability <= 1.0

        device = session.get(Device, "e2e-01")
        assert device is not None
        assert device.location == "E2E Lab"
        assert device.online is True


def test_malformed_payload_over_the_wire_is_rejected_without_killing_ingest(
    broker, subscriber
):
    client = _publisher(broker.port)
    try:
        # A garbage frame, then a good one. The good one must still land.
        client.publish("fireprotect/e2e-02/telemetry", "not json at all", qos=1)
        time.sleep(0.4)
        client.publish(
            "fireprotect/e2e-02/telemetry",
            json.dumps(
                {
                    "temperature_c": 21.0,
                    "humidity_pct": 45.0,
                    "smoke_ppm": 560.0,
                    "air_quality_ppm": 110.0,
                    "flame_analog_volts": 0.15,
                }
            ),
            qos=1,
        )
        assert _wait_for(lambda: _count(Reading) >= 1), "ingest died on a bad payload"
    finally:
        client.loop_stop()
        client.disconnect()

    assert subscriber.stats["rejected"] >= 1


def test_flashover_scenario_end_to_end_raises_a_fire_alert(broker, subscriber):
    """The full DONE-criteria path: simulator -> MQTT -> model -> alert."""
    device = VirtualDevice("e2e-flashover", "flashover", seed=7)
    client = _publisher(broker.port)
    broadcasts: list[tuple[str, dict]] = []
    get_ingest_service().set_broadcaster(
        lambda kind, payload: broadcasts.append((kind, payload))
    )

    try:
        for _ in range(80):
            payload = device.tick(2.0)
            client.publish(
                f"fireprotect/{payload['device_id']}/telemetry",
                json.dumps(payload),
                qos=1,
            )
            time.sleep(0.02)

        assert _wait_for(lambda: _count(Reading) >= 60, timeout=30), (
            "readings did not arrive over MQTT"
        )
        assert _wait_for(
            lambda: any(a.severity == "FIRE" for a in _alerts()), timeout=30
        ), "flashover did not raise a FIRE alert end to end"
    finally:
        client.loop_stop()
        client.disconnect()
        get_ingest_service().set_broadcaster(None)

    statuses = {r.server_status for r in _readings()}
    assert "FIRE" in statuses

    kinds = {kind for kind, _ in broadcasts}
    assert "reading" in kinds, "no reading was broadcast to WebSocket clients"
    assert "alert" in kinds, "no alert was broadcast to WebSocket clients"


def test_normal_scenario_end_to_end_raises_no_fire_alert(broker, subscriber):
    device = VirtualDevice("e2e-normal", "normal", seed=7)
    client = _publisher(broker.port)
    try:
        for _ in range(80):
            payload = device.tick(2.0)
            client.publish(
                f"fireprotect/{payload['device_id']}/telemetry",
                json.dumps(payload),
                qos=1,
            )
            time.sleep(0.02)
        assert _wait_for(lambda: _count(Reading) >= 60, timeout=30)
    finally:
        client.loop_stop()
        client.disconnect()

    assert [a for a in _alerts() if a.severity == "FIRE"] == [], (
        "normal operation raised a false FIRE alert"
    )


def test_multiple_devices_are_kept_separate(broker, subscriber):
    client = _publisher(broker.port)
    try:
        for device_id in ("e2e-a", "e2e-b", "e2e-c"):
            client.publish(
                f"fireprotect/{device_id}/telemetry",
                json.dumps(
                    {
                        "temperature_c": 22.0,
                        "humidity_pct": 45.0,
                        "smoke_ppm": 560.0,
                        "air_quality_ppm": 110.0,
                        "flame_analog_volts": 0.15,
                    }
                ),
                qos=1,
            )
        assert _wait_for(lambda: _count(Device) >= 3)
    finally:
        client.loop_stop()
        client.disconnect()

    with session_scope() as session:
        ids = {d.id for d in session.scalars(select(Device)).all()}
    assert {"e2e-a", "e2e-b", "e2e-c"} <= ids


def _alerts() -> list[Alert]:
    with session_scope() as session:
        return list(session.scalars(select(Alert)).all())


def _readings() -> list[Reading]:
    with session_scope() as session:
        return list(session.scalars(select(Reading)).all())


def test_ingest_service_singleton_is_used_by_the_subscriber(broker, subscriber):
    """Guards against the subscriber quietly using a second, isolated service."""
    assert isinstance(get_ingest_service(), IngestService)
    client = _publisher(broker.port)
    try:
        client.publish(
            "fireprotect/e2e-singleton/telemetry",
            json.dumps(
                {
                    "temperature_c": 22.0,
                    "humidity_pct": 45.0,
                    "smoke_ppm": 560.0,
                    "air_quality_ppm": 110.0,
                    "flame_analog_volts": 0.15,
                }
            ),
            qos=1,
        )
        assert _wait_for(lambda: _count(Reading) >= 1)
    finally:
        client.loop_stop()
        client.disconnect()
