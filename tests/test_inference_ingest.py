"""Inference, risk scoring, ingest state machine and WebSocket hub tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import Alert, Device, Reading, session_scope, utcnow
from app.inference import (
    FireClassifier,
    _threshold_fallback,
    compute_heat_index,
    compute_risk_score,
    get_classifier,
)
from app.ingest import IngestService, get_ingest_service
from app.schemas import TelemetryIn

SAFE = {
    "device_id": "d1",
    "temperature_c": 21.0,
    "humidity_pct": 45.0,
    "smoke_ppm": 100.0,
    "air_quality_ppm": 90.0,
    "flame_analog_volts": 0.02,
    "flame_detected": 0,
}
FIRE = {
    "device_id": "d1",
    "temperature_c": 92.0,
    "humidity_pct": 16.0,
    "smoke_ppm": 4800.0,
    "air_quality_ppm": 1800.0,
    "flame_analog_volts": 3.0,
    "flame_detected": 1,
}


# --------------------------------------------------------------------------
# Heat index / risk score
# --------------------------------------------------------------------------


def test_heat_index_matches_noaa_reference():
    result_f = compute_heat_index(32.222, 70.0) * 9.0 / 5.0 + 32.0
    assert result_f == pytest.approx(105.4, abs=1.5)


def test_heat_index_below_threshold_uses_simple_form():
    assert compute_heat_index(20.0, 50.0) == pytest.approx(20.0, abs=4.0)


def test_risk_score_bounds():
    assert compute_risk_score({"SAFE": 1.0, "WARNING": 0.0, "FIRE": 0.0}) == 0.0
    assert compute_risk_score({"SAFE": 0.0, "WARNING": 0.0, "FIRE": 1.0}) == 100.0


def test_risk_score_weights_warning_at_half():
    assert compute_risk_score({"SAFE": 0.0, "WARNING": 1.0, "FIRE": 0.0}) == 50.0


def test_risk_score_is_monotonic_in_fire_probability():
    scores = [
        compute_risk_score({"SAFE": 1 - p, "WARNING": 0.0, "FIRE": p})
        for p in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert scores == sorted(scores)


def test_risk_score_handles_missing_keys():
    assert compute_risk_score({}) == 0.0


# --------------------------------------------------------------------------
# Threshold fallback
# --------------------------------------------------------------------------


def test_fallback_safe_conditions():
    status, probabilities = _threshold_fallback(
        {"temperature_c": 21.0, "smoke_ppm": 100.0, "air_quality_ppm": 80.0}
    )
    assert status == "SAFE"
    assert probabilities["FIRE"] == 0.0


def test_fallback_flame_plus_heat_is_fire():
    status, _ = _threshold_fallback(
        {"temperature_c": 60.0, "smoke_ppm": 100.0, "flame_analog_volts": 2.0}
    )
    assert status == "FIRE"


def test_fallback_severe_smoke_alone_is_fire():
    status, _ = _threshold_fallback({"temperature_c": 22.0, "smoke_ppm": 3000.0})
    assert status == "FIRE"


def test_fallback_elevated_temp_alone_is_warning():
    status, _ = _threshold_fallback({"temperature_c": 50.0, "smoke_ppm": 100.0})
    assert status == "WARNING"


def test_fallback_probabilities_sum_to_one():
    for values in (
        {"temperature_c": 21.0, "smoke_ppm": 50.0},
        {"temperature_c": 50.0, "smoke_ppm": 50.0},
        {"temperature_c": 90.0, "smoke_ppm": 5000.0},
    ):
        _, probabilities = _threshold_fallback(values)
        assert sum(probabilities.values()) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------


def test_missing_model_directory_degrades_gracefully(tmp_path):
    classifier = FireClassifier(model_dir=tmp_path)
    assert classifier.load() is False
    assert classifier.loaded is False
    # Still classifies, via the fallback rules.
    status, _ = classifier.predict({"temperature_c": 95.0, "smoke_ppm": 5000.0})
    assert status == "FIRE"


def test_corrupt_model_file_degrades_gracefully(tmp_path):
    (tmp_path / "random_forest.joblib").write_bytes(b"this is not a joblib file")
    classifier = FireClassifier(model_dir=tmp_path)
    assert classifier.load() is False


def test_trained_model_is_available():
    """The committed model must load - the pipeline depends on it."""
    assert get_classifier().loaded, "run `python ml/train.py` to build the model"


def test_classifier_feature_vector_shape():
    classifier = get_classifier()
    features = classifier.build_features({"temperature_c": 22.0})
    assert features.shape == (1, 9)
    assert features[0][0] == 22.0


def test_classifier_predicts_fire_on_fire_conditions():
    status, probabilities = get_classifier().predict(
        {
            "temperature_c": 95.0,
            "humidity_pct": 15.0,
            "smoke_ppm": 5000.0,
            "air_quality_ppm": 2000.0,
            "flame_analog_volts": 3.0,
            "flame_detected": 1.0,
            "temp_rate_c_per_min": 40.0,
            "smoke_rate_ppm_per_min": 1200.0,
            "heat_index_c": 100.0,
        }
    )
    assert status == "FIRE"
    assert probabilities["FIRE"] > 0.5


def test_classifier_predicts_safe_on_quiescent_conditions():
    status, _ = get_classifier().predict(
        {
            "temperature_c": 21.5,
            "humidity_pct": 45.0,
            "smoke_ppm": 105.0,
            "air_quality_ppm": 92.0,
            "flame_analog_volts": 0.02,
            "flame_detected": 0.0,
            "temp_rate_c_per_min": 0.1,
            "smoke_rate_ppm_per_min": 0.5,
            "heat_index_c": 21.0,
        }
    )
    assert status == "SAFE"


# --------------------------------------------------------------------------
# Ingest service
# --------------------------------------------------------------------------


def _ingest(service: IngestService, payload: dict) -> dict:
    return service.process_telemetry(TelemetryIn.model_validate(payload))


def test_ingest_creates_device_and_reading():
    service = get_ingest_service()
    result = _ingest(service, SAFE)
    assert result["device_id"] == "d1"
    with session_scope() as session:
        assert session.get(Device, "d1") is not None
        assert len(session.scalars(select(Reading)).all()) == 1


def test_ingest_is_idempotent_about_device_creation():
    service = get_ingest_service()
    _ingest(service, SAFE)
    _ingest(service, SAFE)
    with session_scope() as session:
        assert len(session.scalars(select(Device)).all()) == 1


def test_fire_alert_requires_consecutive_readings():
    service = get_ingest_service()
    settings = get_settings()
    for _ in range(settings.fire_alert_consecutive - 1):
        _ingest(service, FIRE)
    with session_scope() as session:
        assert session.scalars(select(Alert)).all() == []

    _ingest(service, FIRE)
    with session_scope() as session:
        alerts = session.scalars(select(Alert)).all()
        assert len(alerts) == 1
        assert alerts[0].severity == "FIRE"


def test_repeated_fire_does_not_duplicate_alerts():
    service = get_ingest_service()
    for _ in range(10):
        _ingest(service, FIRE)
    with session_scope() as session:
        assert len(session.scalars(select(Alert)).all()) == 1


def test_safe_readings_resolve_the_alert():
    service = get_ingest_service()
    for _ in range(3):
        _ingest(service, FIRE)
    for _ in range(3):
        _ingest(service, SAFE)
    with session_scope() as session:
        alert = session.scalars(select(Alert)).first()
        assert alert is not None
        assert alert.resolved_at is not None


def test_alert_message_names_the_triggering_conditions():
    service = get_ingest_service()
    for _ in range(3):
        _ingest(service, FIRE)
    with session_scope() as session:
        alert = session.scalars(select(Alert)).first()
        assert alert is not None
        assert "flame detected" in alert.message
        assert "P(fire)=" in alert.message


def test_rate_features_derived_across_samples():
    service = get_ingest_service()
    _ingest(service, SAFE)
    second = _ingest(service, {**SAFE, "temperature_c": 40.0})
    assert second["temp_rate_c_per_min"] > 0.0


def test_rate_features_are_per_device():
    service = get_ingest_service()
    _ingest(service, SAFE)
    other = _ingest(service, {**SAFE, "device_id": "d2", "temperature_c": 80.0})
    # d2's first reading must not inherit d1's history.
    assert other["temp_rate_c_per_min"] == 0.0


def test_broadcast_receives_reading_and_alert_events():
    service = IngestService()
    events: list[tuple[str, dict]] = []
    service.set_broadcaster(lambda kind, payload: events.append((kind, payload)))

    for _ in range(3):
        service.process_telemetry(TelemetryIn.model_validate(FIRE))

    kinds = [kind for kind, _ in events]
    assert "device" in kinds
    assert "reading" in kinds
    assert "alert" in kinds


def test_broadcast_failure_does_not_break_ingest():
    service = IngestService()

    def exploding(_kind, _payload):
        raise RuntimeError("websocket layer is on fire")

    service.set_broadcaster(exploding)
    result = service.process_telemetry(TelemetryIn.model_validate(SAFE))
    assert result["device_id"] == "d1"


def test_thingspeak_failure_does_not_break_ingest(monkeypatch):
    from app import ingest as ingest_module

    class Exploding:
        def submit(self, _reading):
            raise RuntimeError("cloud is down")

    monkeypatch.setattr(ingest_module, "get_mirror", lambda: Exploding())
    service = IngestService()
    result = service.process_telemetry(TelemetryIn.model_validate(SAFE))
    assert result["device_id"] == "d1"


def test_stale_devices_are_marked_offline():
    service = get_ingest_service()
    _ingest(service, SAFE)
    with session_scope() as session:
        device = session.get(Device, "d1")
        assert device is not None
        device.last_seen = utcnow() - timedelta(hours=1)

    changed = service.mark_stale_devices_offline()
    assert "d1" in changed
    with session_scope() as session:
        assert session.get(Device, "d1").online is False


def test_recent_devices_stay_online():
    service = get_ingest_service()
    _ingest(service, SAFE)
    assert service.mark_stale_devices_offline() == []


def test_device_coming_back_is_marked_online():
    service = get_ingest_service()
    _ingest(service, SAFE)
    with session_scope() as session:
        session.get(Device, "d1").last_seen = utcnow() - timedelta(hours=1)
    service.mark_stale_devices_offline()

    _ingest(service, SAFE)
    with session_scope() as session:
        assert session.get(Device, "d1").online is True


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


def test_websocket_receives_hello_then_live_readings(client):
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["payload"]["protocol"] == 1

        client.post("/api/telemetry", json=SAFE)
        message = websocket.receive_json()
        assert message["type"] in {"device", "reading"}


def test_websocket_broadcasts_alerts(client):
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()  # hello
        for _ in range(3):
            client.post("/api/telemetry", json=FIRE)

        seen: list[str] = []
        for _ in range(12):
            seen.append(websocket.receive_json()["type"])
            if "alert" in seen:
                break
        assert "alert" in seen


def test_hub_broadcast_with_no_clients_is_a_noop():
    import asyncio

    from app.ws import ConnectionHub

    asyncio.run(ConnectionHub().broadcast("reading", {"a": 1}))


def test_hub_threadsafe_broadcast_without_loop_is_a_noop():
    from app.ws import ConnectionHub

    ConnectionHub().broadcast_threadsafe("reading", {"a": 1})
