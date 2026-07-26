"""Camera-node detection and ingest.

The scoring tests are written as scenarios rather than assertions on constants,
so retuning a threshold in ``app.camera`` does not force a rewrite here — but
inverting the detector's behaviour does fail, which is the point.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import camera
from app.camera import CameraClassifier, compute_flicker, describe, score


def _frame(**overrides: float) -> dict[str, float]:
    base = {
        "flame_ratio": 0.0,
        "luminance": 0.35,
        "haze_index": 0.20,
        "haze_baseline": 0.20,
        "flicker": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Flicker
# ---------------------------------------------------------------------------


def test_flicker_needs_history() -> None:
    assert compute_flicker([]) == 0.0
    assert compute_flicker([0.02, 0.03]) == 0.0


def test_flicker_zero_for_a_perfectly_steady_signal() -> None:
    # Not exactly 0.0: summing ten copies of 0.03 leaves float noise around
    # 1e-17, which is many orders below anything the thresholds react to.
    assert compute_flicker([0.03] * 10) < 1e-9


def test_flicker_zero_when_nothing_flame_like_is_present() -> None:
    """An all-dark window has no meaningful variability to report."""
    assert compute_flicker([0.0] * 10) == 0.0


def test_flicker_is_scale_free() -> None:
    """A near and a distant flame flicker by different absolute amounts.

    Both should score the same, otherwise one threshold could not work at two
    distances -- which is the whole reason it is a coefficient of variation.
    """
    near = [0.10, 0.20, 0.10, 0.20, 0.10, 0.20]
    far = [0.01, 0.02, 0.01, 0.02, 0.01, 0.02]
    assert compute_flicker(near) == pytest.approx(compute_flicker(far), rel=1e-9)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_empty_scene_is_safe() -> None:
    status, probabilities, _ = score(**_frame())
    assert status == "SAFE"
    assert probabilities["FIRE"] == 0.0
    assert abs(sum(probabilities.values()) - 1.0) < 1e-9


def test_flickering_flame_is_fire() -> None:
    status, probabilities, evidence = score(
        **_frame(flame_ratio=0.06, luminance=0.5, flicker=0.5)
    )
    assert status == "FIRE"
    assert probabilities["FIRE"] > 0.55
    assert evidence.flame_confidence > 0.8


def test_static_orange_object_does_not_reach_fire() -> None:
    """A red jumper, a sunset or a lit EXIT sign fills the frame but is still.

    This is the single most important negative case: without the flicker term
    the feature would alarm on furniture.
    """
    status, probabilities, _ = score(
        **_frame(flame_ratio=0.35, luminance=0.6, flicker=0.0)
    )
    assert status != "FIRE"
    assert probabilities["FIRE"] < 0.55


def test_static_orange_object_still_warns() -> None:
    """Not FIRE, but not silence either -- it is worth a look."""
    status, _, _ = score(**_frame(flame_ratio=0.35, flicker=0.0))
    assert status == "WARNING"


def test_haze_alone_warns_but_never_fires() -> None:
    """Steam from a kettle must not be reported as flame."""
    status, probabilities, evidence = score(
        **_frame(haze_index=0.60, haze_baseline=0.20)
    )
    assert status == "WARNING"
    assert probabilities["FIRE"] == 0.0
    assert evidence.smoke_confidence == 1.0


def test_haze_amplifies_an_existing_flame_signal() -> None:
    without = score(**_frame(flame_ratio=0.03, flicker=0.4))[1]["FIRE"]
    with_haze = score(**_frame(flame_ratio=0.03, flicker=0.4, haze_index=0.45))[1][
        "FIRE"
    ]
    assert with_haze > without


def test_noise_floor_rejects_a_handful_of_warm_pixels() -> None:
    status, probabilities, _ = score(**_frame(flame_ratio=0.001, flicker=0.9))
    assert status == "SAFE"
    assert probabilities["FIRE"] == 0.0


def test_darkness_does_not_suppress_detection() -> None:
    """A fire at night is still a fire; luminance is context, not a gate."""
    status, _, _ = score(**_frame(flame_ratio=0.06, luminance=0.02, flicker=0.5))
    assert status == "FIRE"


def test_probabilities_always_form_a_distribution() -> None:
    for flame in (0.0, 0.01, 0.05, 0.4, 1.0):
        for flicker in (0.0, 0.15, 0.6):
            for haze in (0.0, 0.3, 1.0):
                _, probabilities, _ = score(
                    flame_ratio=flame,
                    luminance=0.4,
                    haze_index=haze,
                    haze_baseline=0.1,
                    flicker=flicker,
                )
                assert abs(sum(probabilities.values()) - 1.0) < 1e-9
                assert all(0.0 <= p <= 1.0 for p in probabilities.values())


def test_non_finite_inputs_are_neutralised() -> None:
    """NaN must not propagate into a probability the gauge then renders."""
    status, probabilities, _ = score(
        flame_ratio=float("nan"),
        luminance=float("inf"),
        haze_index=float("nan"),
        haze_baseline=0.1,
        flicker=float("nan"),
    )
    assert status == "SAFE"
    assert all(p == p for p in probabilities.values())


# ---------------------------------------------------------------------------
# Stateful classifier
# ---------------------------------------------------------------------------


def test_baseline_adopts_the_first_frame() -> None:
    classifier = CameraClassifier()
    _, _, evidence = classifier.classify("cam-1", 0.0, 0.4, 0.55)
    assert evidence.haze_baseline == 0.55
    assert evidence.smoke_confidence == 0.0


def test_permanently_soft_lens_settles_at_no_smoke() -> None:
    """A blurry webcam must not alarm forever."""
    classifier = CameraClassifier()
    for _ in range(50):
        status, _, evidence = classifier.classify("cam-1", 0.0, 0.4, 0.7)
    assert status == "SAFE"
    assert evidence.smoke_confidence == 0.0


def test_rising_haze_outruns_the_baseline() -> None:
    """The asymmetric baseline exists so gradual smoke is still detected."""
    classifier = CameraClassifier()
    for _ in range(30):
        classifier.classify("cam-1", 0.0, 0.4, 0.20)
    haze = 0.20
    for _ in range(20):
        haze += 0.02
        status, _, evidence = classifier.classify("cam-1", 0.0, 0.4, haze)
    assert evidence.smoke_confidence > 0.5
    assert status == "WARNING"


def test_clearing_haze_is_adopted_quickly() -> None:
    classifier = CameraClassifier()
    for _ in range(30):
        classifier.classify("cam-1", 0.0, 0.4, 0.60)
    for _ in range(60):
        _, _, evidence = classifier.classify("cam-1", 0.0, 0.4, 0.15)
    assert evidence.haze_baseline < 0.25


def test_devices_do_not_share_history() -> None:
    classifier = CameraClassifier()
    for value in (0.02, 0.30, 0.02, 0.30, 0.02, 0.30):
        classifier.classify("cam-a", value, 0.4, 0.2)
    _, _, evidence = classifier.classify("cam-b", 0.30, 0.4, 0.2)
    assert evidence.flicker == 0.0


def test_a_developing_flame_escalates_over_time() -> None:
    """The realistic path: nothing, then a flicker, then unmistakable."""
    classifier = CameraClassifier()
    for _ in range(8):
        status, _, _ = classifier.classify("cam-1", 0.0, 0.3, 0.2)
    assert status == "SAFE"

    for index in range(12):
        flame = 0.05 + (0.03 if index % 2 else -0.02)
        status, _, _ = classifier.classify("cam-1", flame, 0.4, 0.2)
    assert status == "FIRE"


def test_reset_clears_one_device_only() -> None:
    classifier = CameraClassifier()
    classifier.classify("cam-a", 0.1, 0.4, 0.5)
    classifier.classify("cam-b", 0.1, 0.4, 0.5)
    classifier.reset("cam-a")
    _, _, evidence = classifier.classify("cam-b", 0.1, 0.4, 0.9)
    assert evidence.haze_baseline < 0.9  # cam-b kept its baseline


# ---------------------------------------------------------------------------
# Alert wording
# ---------------------------------------------------------------------------


def test_fire_description_quantifies_the_evidence() -> None:
    _, _, evidence = score(**_frame(flame_ratio=0.08, flicker=0.5))
    text = describe("FIRE", evidence)
    assert "flame" in text.lower()
    assert "%" in text


def test_warning_description_never_claims_a_gas_reading() -> None:
    """A camera alert must not be worded as though a sensor smelled something."""
    _, _, evidence = score(**_frame(haze_index=0.6, haze_baseline=0.2))
    text = describe("WARNING", evidence)
    assert "ppm" not in text.lower()
    assert "haze" in text.lower()


# ---------------------------------------------------------------------------
# Ingest through the API
# ---------------------------------------------------------------------------


def _post_frame(client: TestClient, **overrides: object) -> dict:
    payload = {
        "device_id": "cam-test-01",
        "flame_ratio": 0.0,
        "luminance": 0.4,
        "haze_index": 0.2,
        "location": "Test Room",
    }
    payload.update(overrides)
    response = client.post("/api/camera/telemetry", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_camera_reading_is_tagged_and_leaves_gas_channels_null(
    client: TestClient,
) -> None:
    """The core honesty guarantee: no fabricated ppm, ever."""
    body = _post_frame(client)
    assert body["sensor_kind"] == "camera"
    assert body["smoke_ppm"] is None
    assert body["air_quality_ppm"] is None
    assert body["flame_analog_volts"] is None
    assert body["flame_ratio"] is not None
    assert body["haze_index"] is not None


def test_camera_device_is_registered_with_its_kind(client: TestClient) -> None:
    _post_frame(client)
    devices = client.get("/api/devices").json()
    assert len(devices) == 1
    assert devices[0]["kind"] == "camera"
    assert devices[0]["location"] == "Test Room"


def test_camera_flicker_is_derived_not_trusted(client: TestClient) -> None:
    """A client cannot claim flicker; the server computes it from history."""
    body = _post_frame(client, flame_ratio=0.4, flicker=0.99)
    assert body["flicker"] == 0.0
    assert body["server_status"] != "FIRE"


def test_camera_weather_temperature_is_stored_when_supplied(
    client: TestClient,
) -> None:
    body = _post_frame(client, temperature_c=21.5, humidity_pct=48.0)
    assert body["temperature_c"] == 21.5
    assert body["humidity_pct"] == 48.0
    assert body["heat_index_c"] != 0.0


def test_camera_temperature_stays_null_when_not_shared(client: TestClient) -> None:
    body = _post_frame(client)
    assert body["temperature_c"] is None
    assert body["humidity_pct"] is None


def test_camera_rejects_out_of_range_metrics(client: TestClient) -> None:
    response = client.post(
        "/api/camera/telemetry",
        json={
            "device_id": "cam-test-01",
            "flame_ratio": 1.5,
            "luminance": 0.4,
            "haze_index": 0.2,
        },
    )
    assert response.status_code == 422


def test_camera_rejects_nan(client: TestClient) -> None:
    response = client.post(
        "/api/camera/telemetry",
        content=(
            b'{"device_id":"cam-test-01","flame_ratio":NaN,'
            b'"luminance":0.4,"haze_index":0.2}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_sustained_flame_raises_an_alert_worded_for_a_camera(
    client: TestClient,
) -> None:
    for index in range(16):
        _post_frame(client, flame_ratio=0.06 + (0.03 if index % 2 else -0.02))

    alerts = client.get("/api/alerts").json()
    assert alerts, "a flickering flame should have raised an alert"
    assert alerts[0]["severity"] == "FIRE"
    assert "ppm" not in alerts[0]["message"].lower()
    assert "camera" in alerts[0]["message"].lower()


def test_camera_and_hardware_nodes_coexist(
    client: TestClient, sample_telemetry: dict
) -> None:
    client.post("/api/telemetry", json=sample_telemetry)
    _post_frame(client)

    devices = {device["id"]: device for device in client.get("/api/devices").json()}
    assert devices["esp32-test-01"]["kind"] == "hardware"
    assert devices["cam-test-01"]["kind"] == "camera"

    readings = client.get("/api/readings").json()
    kinds = {reading["device_id"]: reading["sensor_kind"] for reading in readings}
    assert kinds["esp32-test-01"] == "hardware"
    assert kinds["cam-test-01"] == "camera"


def test_hardware_readings_keep_their_camera_columns_null(
    client: TestClient, sample_telemetry: dict
) -> None:
    """Symmetry: an ESP32 has no flicker score, and must not claim one."""
    body = client.post("/api/telemetry", json=sample_telemetry).json()
    assert body["flame_ratio"] is None
    assert body["haze_index"] is None
    assert body["flicker"] is None
    assert body["smoke_ppm"] is not None


def test_camera_classifier_singleton_resets(client: TestClient) -> None:
    _post_frame(client, flame_ratio=0.3)
    camera.reset_camera_classifier()
    body = _post_frame(client, flame_ratio=0.3)
    assert body["flicker"] == 0.0
