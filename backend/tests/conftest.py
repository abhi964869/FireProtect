"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import camera, inference, ingest, mqtt_client, thingspeak  # noqa: E402
from app import db as db_module  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a private database, buffer file and singletons.

    Without this, tests leak state into each other through the module-level
    singletons and the on-disk SQLite file, producing order-dependent failures.
    """
    monkeypatch.setenv("THINGSPEAK_BUFFER_PATH", str(tmp_path / "buffer.jsonl"))
    monkeypatch.setenv("THINGSPEAK_ENABLED", "false")
    get_settings.cache_clear()

    settings = get_settings()
    settings.thingspeak_buffer_path = tmp_path / "buffer.jsonl"

    db_module.configure(f"sqlite:///{tmp_path / 'test.db'}")
    db_module.init_db()

    ingest.reset_ingest_service()
    thingspeak.reset_mirror()
    mqtt_client.reset_subscriber()
    # The camera classifier holds per-device flicker history and a haze
    # baseline. Leaking those between tests makes the detector's verdict depend
    # on test order, which is exactly the bug class this fixture exists to kill.
    camera.reset_camera_classifier()

    yield

    ingest.reset_ingest_service()
    thingspeak.reset_mirror()
    mqtt_client.reset_subscriber()
    camera.reset_camera_classifier()
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient with the real lifespan but no live MQTT connection.

    The lifespan otherwise dials a broker on every test, which makes the suite
    depend on external infrastructure and adds seconds of connect/retry per
    test. The MQTT layer is covered directly in ``test_mqtt.py`` by driving
    ``handle_message``, so nothing is lost by stubbing the socket here.
    """
    from app.main import app
    from app.mqtt_client import MqttSubscriber

    monkeypatch.setattr(MqttSubscriber, "start", lambda self: False)
    monkeypatch.setattr(MqttSubscriber, "stop", lambda self: None)

    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


@pytest.fixture
def no_lifespan_client() -> Iterator[TestClient]:
    """TestClient with lifespan suppressed, for pure-handler tests."""
    from app.main import app

    original = app.router.lifespan_context

    async def _noop(_app):  # type: ignore[no-untyped-def]
        yield

    app.router.lifespan_context = _noop  # type: ignore[assignment]
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.router.lifespan_context = original  # type: ignore[assignment]


@pytest.fixture
def sample_telemetry() -> dict:
    return {
        "device_id": "esp32-test-01",
        "temperature_c": 22.4,
        "humidity_pct": 45.2,
        "smoke_ppm": 120.0,
        "air_quality_ppm": 95.0,
        "flame_analog_volts": 0.05,
        "flame_detected": 0,
        "device_status": "SAFE",
        "firmware_version": "1.0.0",
        "location": "Test Lab",
    }


@pytest.fixture
def fire_telemetry() -> dict:
    return {
        "device_id": "esp32-test-01",
        "temperature_c": 88.0,
        "humidity_pct": 18.0,
        "smoke_ppm": 4200.0,
        "air_quality_ppm": 1400.0,
        "flame_analog_volts": 2.9,
        "flame_detected": 1,
        "device_status": "FIRE",
        "firmware_version": "1.0.0",
        "location": "Test Lab",
    }


@pytest.fixture
def classifier_loaded() -> bool:
    inference.reset_classifier()
    return inference.get_classifier().loaded
