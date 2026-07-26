"""REST API contract tests - every endpoint, and its response shape."""

from __future__ import annotations

import pytest

from app.schemas import AlertOut, DeviceOut, ReadingOut


def _post(client, payload):
    response = client.post("/api/telemetry", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Health / stats
# --------------------------------------------------------------------------


def test_health_shape(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "status",
        "database",
        "mqtt_connected",
        "model_loaded",
        "thingspeak_pending",
        "version",
    }
    assert body["status"] in {"ok", "degraded"}
    assert body["database"] is True


def test_stats_empty_database(client):
    body = client.get("/api/stats").json()
    assert body["total_devices"] == 0
    assert body["total_readings"] == 0
    assert body["open_alerts"] == 0
    assert body["current_status"] == "SAFE"


def test_stats_after_ingest(client, sample_telemetry):
    _post(client, sample_telemetry)
    body = client.get("/api/stats").json()
    assert body["total_devices"] == 1
    assert body["online_devices"] == 1
    assert body["total_readings"] == 1


# --------------------------------------------------------------------------
# Telemetry ingest
# --------------------------------------------------------------------------


def test_ingest_returns_reading_shape(client, sample_telemetry):
    body = _post(client, sample_telemetry)
    ReadingOut.model_validate(body)
    assert body["device_id"] == "esp32-test-01"
    assert body["server_status"] in {"SAFE", "WARNING", "FIRE"}
    assert 0.0 <= body["fire_probability"] <= 1.0
    assert 0.0 <= body["risk_score"] <= 100.0


def test_ingest_creates_device(client, sample_telemetry):
    _post(client, sample_telemetry)
    devices = client.get("/api/devices").json()
    assert len(devices) == 1
    DeviceOut.model_validate(devices[0])
    assert devices[0]["id"] == "esp32-test-01"
    assert devices[0]["location"] == "Test Lab"
    assert devices[0]["online"] is True


def test_ingest_computes_rate_features(client, sample_telemetry):
    _post(client, sample_telemetry)
    hotter = {**sample_telemetry, "temperature_c": 32.4}
    second = _post(client, hotter)
    # 10 C rise between two samples must produce a positive rate.
    assert second["temp_rate_c_per_min"] > 0.0


def test_first_reading_has_zero_rate(client, sample_telemetry):
    body = _post(client, sample_telemetry)
    assert body["temp_rate_c_per_min"] == 0.0
    assert body["smoke_rate_ppm_per_min"] == 0.0


def test_fire_telemetry_classified_as_fire(client, fire_telemetry):
    body = _post(client, fire_telemetry)
    assert body["server_status"] == "FIRE"
    assert body["risk_score"] > 50.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature_c", 1e9),
        ("temperature_c", -500.0),
        ("humidity_pct", 150.0),
        ("humidity_pct", -1.0),
        ("smoke_ppm", -5.0),
        ("flame_analog_volts", 99.0),
        ("flame_detected", 7),
    ],
)
def test_out_of_range_values_rejected(client, sample_telemetry, field, value):
    response = client.post("/api/telemetry", json={**sample_telemetry, field: value})
    assert response.status_code == 422


def test_missing_device_id_rejected(client, sample_telemetry):
    payload = {k: v for k, v in sample_telemetry.items() if k != "device_id"}
    assert client.post("/api/telemetry", json=payload).status_code == 422


def test_unknown_extra_fields_are_ignored(client, sample_telemetry):
    body = _post(client, {**sample_telemetry, "totally_unexpected": "x"})
    assert body["device_id"] == "esp32-test-01"


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


def test_device_detail_shape(client, sample_telemetry):
    _post(client, sample_telemetry)
    body = client.get("/api/devices/esp32-test-01").json()
    assert body["id"] == "esp32-test-01"
    assert body["latest_reading"] is not None
    ReadingOut.model_validate(body["latest_reading"])
    assert body["open_alerts"] == 0


def test_unknown_device_returns_404(client):
    assert client.get("/api/devices/nope").status_code == 404


def test_unknown_device_readings_returns_404(client):
    assert client.get("/api/devices/nope/readings").status_code == 404


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------


def test_readings_list_and_ordering(client, sample_telemetry):
    for temp in (20.0, 21.0, 22.0):
        _post(client, {**sample_telemetry, "temperature_c": temp})
    body = client.get("/api/readings").json()
    assert len(body) == 3
    # Newest first.
    assert body[0]["temperature_c"] == 22.0


def test_readings_limit_is_enforced(client, sample_telemetry):
    for temp in range(20, 25):
        _post(client, {**sample_telemetry, "temperature_c": float(temp)})
    assert len(client.get("/api/readings?limit=2").json()) == 2


def test_readings_limit_bounds_validated(client):
    assert client.get("/api/readings?limit=0").status_code == 422
    assert client.get("/api/readings?limit=99999").status_code == 422


def test_readings_filtered_by_device(client, sample_telemetry):
    _post(client, sample_telemetry)
    _post(client, {**sample_telemetry, "device_id": "esp32-test-02"})
    body = client.get("/api/readings?device_id=esp32-test-02").json()
    assert len(body) == 1
    assert body[0]["device_id"] == "esp32-test-02"


def test_device_readings_endpoint(client, sample_telemetry):
    _post(client, sample_telemetry)
    body = client.get("/api/devices/esp32-test-01/readings").json()
    assert len(body) == 1


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def test_sustained_fire_raises_alert(client, fire_telemetry):
    for _ in range(4):
        _post(client, fire_telemetry)
    alerts = client.get("/api/alerts").json()
    assert len(alerts) >= 1
    AlertOut.model_validate(alerts[0])
    assert alerts[0]["severity"] == "FIRE"
    assert "P(fire)=" in alerts[0]["message"]


def test_single_fire_reading_does_not_alert(client, fire_telemetry):
    """Debounce: one sample must not page anyone."""
    _post(client, fire_telemetry)
    assert client.get("/api/alerts").json() == []


def test_safe_readings_resolve_open_alert(client, fire_telemetry, sample_telemetry):
    for _ in range(4):
        _post(client, fire_telemetry)
    assert len(client.get("/api/alerts?only_open=true").json()) == 1
    for _ in range(3):
        _post(client, sample_telemetry)
    assert client.get("/api/alerts?only_open=true").json() == []


def test_acknowledge_alert(client, fire_telemetry):
    for _ in range(4):
        _post(client, fire_telemetry)
    alert_id = client.get("/api/alerts").json()[0]["id"]
    body = client.post(f"/api/alerts/{alert_id}/acknowledge").json()
    assert body["acknowledged"] is True


def test_resolve_alert(client, fire_telemetry):
    for _ in range(4):
        _post(client, fire_telemetry)
    alert_id = client.get("/api/alerts").json()[0]["id"]
    body = client.post(f"/api/alerts/{alert_id}/resolve").json()
    assert body["resolved_at"] is not None


def test_acknowledge_unknown_alert_404(client):
    assert client.post("/api/alerts/999/acknowledge").status_code == 404


def test_resolve_unknown_alert_404(client):
    assert client.post("/api/alerts/999/resolve").status_code == 404


def test_alerts_filtered_by_device(client, fire_telemetry):
    for _ in range(4):
        _post(client, fire_telemetry)
    assert len(client.get("/api/alerts?device_id=esp32-test-01").json()) >= 1
    assert client.get("/api/alerts?device_id=other").json() == []


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def test_default_thresholds_seeded(client):
    body = client.get("/api/thresholds").json()
    keys = {row["key"] for row in body}
    assert "temperature_fire_c" in keys
    assert "smoke_warning_ppm" in keys
    assert len(body) == 7


def test_update_threshold(client):
    response = client.put(
        "/api/thresholds", json={"thresholds": {"temperature_fire_c": 65.0}}
    )
    assert response.status_code == 200
    values = {row["key"]: row["value"] for row in response.json()}
    assert values["temperature_fire_c"] == 65.0


def test_unknown_threshold_key_rejected(client):
    response = client.put("/api/thresholds", json={"thresholds": {"bogus_key": 1.0}})
    assert response.status_code == 422
    assert "bogus_key" in response.json()["detail"]


def test_negative_threshold_rejected(client):
    response = client.put(
        "/api/thresholds", json={"thresholds": {"temperature_fire_c": -5.0}}
    )
    assert response.status_code == 422


def test_empty_threshold_update_rejected(client):
    assert client.put("/api/thresholds", json={"thresholds": {}}).status_code == 422


# --------------------------------------------------------------------------
# Prediction
# --------------------------------------------------------------------------


def test_predict_safe_conditions(client):
    body = client.post(
        "/api/predict",
        json={
            "temperature_c": 21.0,
            "humidity_pct": 45.0,
            "smoke_ppm": 100.0,
            "air_quality_ppm": 90.0,
            "flame_analog_volts": 0.02,
            "flame_detected": 0,
        },
    ).json()
    assert body["status"] == "SAFE"
    assert body["fire_probability"] < 0.5


def test_predict_fire_conditions(client):
    body = client.post(
        "/api/predict",
        json={
            "temperature_c": 95.0,
            "humidity_pct": 15.0,
            "smoke_ppm": 5000.0,
            "air_quality_ppm": 2000.0,
            "flame_analog_volts": 3.0,
            "flame_detected": 1,
            "temp_rate_c_per_min": 30.0,
            "smoke_rate_ppm_per_min": 900.0,
        },
    ).json()
    assert body["status"] == "FIRE"
    assert body["fire_probability"] > 0.5


def test_predict_probabilities_sum_to_one(client):
    body = client.post(
        "/api/predict",
        json={
            "temperature_c": 50.0,
            "humidity_pct": 30.0,
            "smoke_ppm": 1000.0,
            "air_quality_ppm": 400.0,
            "flame_analog_volts": 0.5,
            "flame_detected": 0,
        },
    ).json()
    assert sum(body["probabilities"].values()) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


def test_prune_readings_keeps_recent(client, sample_telemetry):
    _post(client, sample_telemetry)
    body = client.request(
        "DELETE", "/api/readings", params={"older_than_days": 30}
    ).json()
    assert body["deleted"] == 0
    assert len(client.get("/api/readings").json()) == 1


def test_openapi_schema_is_served(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "FireProtect API"
    for path in (
        "/api/health",
        "/api/stats",
        "/api/devices",
        "/api/readings",
        "/api/alerts",
        "/api/thresholds",
        "/api/predict",
        "/api/telemetry",
    ):
        assert path in schema["paths"], f"{path} missing from OpenAPI schema"


# --------------------------------------------------------------------------
# Security: WebSocket origin (cross-site WebSocket hijacking)
# --------------------------------------------------------------------------


def test_websocket_accepts_request_without_origin(client):
    """Non-browser clients (ESP32, curl, tests) send no Origin and must work."""
    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_json()["type"] == "hello"


def test_websocket_accepts_allowed_origin(client):
    with client.websocket_connect(
        "/ws", headers={"origin": "http://localhost:5173"}
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"


def test_websocket_accepts_same_origin(client):
    """The dashboard is served by this app, so its Origin matches Host."""
    with client.websocket_connect(
        "/ws", headers={"origin": "http://testserver", "host": "testserver"}
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"


def test_websocket_rejects_foreign_origin(client):
    """A malicious page must not be able to read the live sensor stream."""
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/ws", headers={"origin": "http://evil.example.com"}
        ) as websocket,
    ):
        websocket.receive_json()
    assert excinfo.value.code == 1008


def test_api_binds_loopback_by_default():
    """The API is unauthenticated; it must not default to every interface."""
    from app.config import Settings

    assert Settings().api_host == "127.0.0.1"
