"""Scenario tests - each simulator scenario must reach the right alert state.

This is the end-to-end behavioural contract: simulator -> ingest -> RF
inference -> DB -> alert. It runs the real pipeline with the real trained model
and no broker, so it is fast and deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "simulator"))

from app.db import Alert, Reading, session_scope  # noqa: E402  # isort: skip
from virtual_device import SCENARIOS, VirtualDevice  # noqa: E402

from app.ingest import IngestService  # noqa: E402
from app.schemas import TelemetryIn  # noqa: E402


def _run(scenario: str, ticks: int, seed: int = 7) -> list[dict]:
    """Drive one scenario through the full ingest pipeline.

    All ticks share one session so the run commits once instead of once per
    reading. Per-reading commits made these tests fsync-bound (~70 ms each);
    the ingest logic under test is identical either way.
    """
    device = VirtualDevice(f"esp32-{scenario}", scenario, seed=seed)
    service = IngestService()
    results = []
    with session_scope() as session:
        for _ in range(ticks):
            payload = device.tick(2.0)
            telemetry = TelemetryIn.model_validate(payload)
            results.append(service.process_telemetry(telemetry, session))
    return results


def _statuses(results: list[dict]) -> list[str]:
    return [row["server_status"] for row in results]


def _alerts() -> list[Alert]:
    with session_scope() as session:
        return list(session.scalars(select(Alert)).all())


# --------------------------------------------------------------------------
# Generator sanity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_payload_is_valid_telemetry(scenario):
    device = VirtualDevice("esp32-x", scenario, seed=1)
    for _ in range(20):
        TelemetryIn.model_validate(device.tick(2.0))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_payload_values_stay_physical(scenario):
    device = VirtualDevice("esp32-x", scenario, seed=2)
    for _ in range(150):
        payload = device.tick(2.0)
        assert 0.0 <= payload["humidity_pct"] <= 100.0
        assert 0.0 <= payload["flame_analog_volts"] <= 3.3
        assert payload["smoke_ppm"] > 0.0
        assert payload["flame_detected"] in (0, 1)
        assert -40.0 <= payload["temperature_c"] <= 300.0


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError, match="scenario must be one of"):
        VirtualDevice("esp32-x", "volcano")


def test_same_seed_produces_same_stream():
    a = VirtualDevice("d", "flashover", seed=42)
    b = VirtualDevice("d", "flashover", seed=42)
    for _ in range(30):
        assert a.tick(2.0) == b.tick(2.0)


# --------------------------------------------------------------------------
# Scenario 1: normal - must stay out of FIRE
# --------------------------------------------------------------------------


def test_normal_scenario_never_reaches_fire():
    """The whole point of the nuisance events is that they must not alarm."""
    results = _run("normal", ticks=200)
    statuses = _statuses(results)
    assert "FIRE" not in statuses, "normal operation produced a FIRE classification"


def test_normal_scenario_raises_no_fire_alert():
    _run("normal", ticks=200)
    assert [a for a in _alerts() if a.severity == "FIRE"] == []


def test_normal_scenario_persists_readings():
    _run("normal", ticks=30)
    with session_scope() as session:
        assert len(session.scalars(select(Reading)).all()) == 30


# --------------------------------------------------------------------------
# Scenario 2: smoldering - must escalate
# --------------------------------------------------------------------------


def test_smoldering_scenario_escalates_beyond_safe():
    results = _run("smoldering", ticks=160)
    statuses = _statuses(results)
    assert statuses[0] == "SAFE", "smoldering should start from a clean baseline"
    assert {"WARNING", "FIRE"} & set(statuses), "smoldering never escalated"


def test_smoldering_scenario_raises_an_alert():
    _run("smoldering", ticks=160)
    alerts = _alerts()
    assert alerts, "smoldering fire produced no alert"
    assert {a.severity for a in alerts} & {"WARNING", "FIRE"}


def test_smoldering_smoke_rises_monotonically_overall():
    results = _run("smoldering", ticks=160)
    early = sum(r["smoke_ppm"] for r in results[:20]) / 20
    late = sum(r["smoke_ppm"] for r in results[-20:]) / 20
    assert late > early * 2, "smoke did not build during a smoldering fire"


# --------------------------------------------------------------------------
# Scenario 3: flashover - must reach FIRE
# --------------------------------------------------------------------------


def test_flashover_scenario_reaches_fire():
    results = _run("flashover", ticks=90)
    assert "FIRE" in _statuses(results), "flashover never classified as FIRE"


def test_flashover_scenario_raises_fire_alert():
    _run("flashover", ticks=90)
    fire_alerts = [a for a in _alerts() if a.severity == "FIRE"]
    assert fire_alerts, "flashover produced no FIRE alert"
    assert "P(fire)=" in fire_alerts[0].message


def test_flashover_starts_safe_then_escalates():
    """Ordering matters: a detector that alarms immediately is useless."""
    results = _run("flashover", ticks=90)
    statuses = _statuses(results)
    first_fire = statuses.index("FIRE")
    assert statuses[0] == "SAFE"
    assert first_fire > 3, "alarmed implausibly early"


def test_flashover_risk_score_climbs():
    results = _run("flashover", ticks=90)
    assert results[0]["risk_score"] < 20.0
    assert max(r["risk_score"] for r in results) > 80.0


def test_flashover_temperature_and_flame_rise():
    results = _run("flashover", ticks=90)
    assert results[-1]["temperature_c"] > results[0]["temperature_c"] + 20
    assert max(r["flame_analog_volts"] for r in results) > 1.5


# --------------------------------------------------------------------------
# Cross-scenario
# --------------------------------------------------------------------------


def test_scenarios_are_distinguishable():
    """The three scenarios must not collapse into the same behaviour."""
    normal = set(_statuses(_run("normal", ticks=120, seed=3)))
    flashover = set(_statuses(_run("flashover", ticks=90, seed=3)))
    assert "FIRE" in flashover
    assert "FIRE" not in normal
