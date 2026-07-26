"""The ingest pipeline: telemetry in, persisted reading + alerts + broadcast out.

This is the single place where a telemetry message becomes state. Both the MQTT
subscriber and the REST ingest endpoint route through ``process_telemetry`` so
the two paths cannot drift apart.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Alert, Device, Reading, Threshold, session_scope, utcnow
from app.inference import compute_heat_index, compute_risk_score, get_classifier
from app.schemas import TelemetryIn
from app.thingspeak import get_mirror

logger = logging.getLogger(__name__)

#: Severity ordering, used to decide whether a state change is an escalation.
SEVERITY_RANK = {"SAFE": 0, "WARNING": 1, "FIRE": 2}


@dataclass
class _DeviceState:
    """Per-device rolling state used to derive rate features and debounce alerts.

    Rate-of-change features cannot be computed from a single message, and the
    models depend on them heavily, so the backend keeps the previous sample in
    memory rather than round-tripping the database on every reading.
    """

    last_temperature: float | None = None
    last_smoke: float | None = None
    last_timestamp: datetime | None = None
    consecutive: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)  # type: ignore[arg-type]
    )
    recent_statuses: deque[str] = field(default_factory=lambda: deque(maxlen=10))


class IngestService:
    """Owns per-device state and turns telemetry into persisted rows."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _DeviceState] = {}
        self._broadcast: Any | None = None

    def set_broadcaster(self, broadcast: Any) -> None:
        """Register the WebSocket hub. Optional - ingest works without it."""
        self._broadcast = broadcast

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    # -- feature derivation ------------------------------------------------

    def _derive(
        self, telemetry: TelemetryIn, now: datetime
    ) -> tuple[dict[str, float], _DeviceState]:
        with self._lock:
            state = self._states.setdefault(telemetry.device_id, _DeviceState())

            temp_rate = 0.0
            smoke_rate = 0.0
            if state.last_timestamp is not None:
                elapsed_min = (now - state.last_timestamp).total_seconds() / 60.0
                # Guard against a zero or negative interval from a device with a
                # skewed clock or a duplicate message.
                if elapsed_min > 1e-6:
                    if state.last_temperature is not None:
                        temp_rate = (
                            telemetry.temperature_c - state.last_temperature
                        ) / elapsed_min
                    if state.last_smoke is not None:
                        smoke_rate = (
                            telemetry.smoke_ppm - state.last_smoke
                        ) / elapsed_min

            state.last_temperature = telemetry.temperature_c
            state.last_smoke = telemetry.smoke_ppm
            state.last_timestamp = now

        features = {
            "temperature_c": telemetry.temperature_c,
            "humidity_pct": telemetry.humidity_pct,
            "smoke_ppm": telemetry.smoke_ppm,
            "air_quality_ppm": telemetry.air_quality_ppm,
            "flame_analog_volts": telemetry.flame_analog_volts,
            "flame_detected": float(telemetry.flame_detected),
            "temp_rate_c_per_min": round(temp_rate, 4),
            "smoke_rate_ppm_per_min": round(smoke_rate, 4),
            "heat_index_c": round(
                compute_heat_index(telemetry.temperature_c, telemetry.humidity_pct), 3
            ),
        }
        return features, state

    # -- main entry point --------------------------------------------------

    def process_telemetry(
        self, telemetry: TelemetryIn, session: Session | None = None
    ) -> dict[str, Any]:
        """Persist one telemetry sample and return the resulting reading dict."""
        if session is not None:
            return self._process(telemetry, session)
        with session_scope() as own_session:
            return self._process(telemetry, own_session)

    def _process(self, telemetry: TelemetryIn, session: Session) -> dict[str, Any]:
        now = utcnow()
        features, state = self._derive(telemetry, now)

        classifier = get_classifier()
        status, probabilities = classifier.predict(features)
        fire_probability = probabilities.get("FIRE", 0.0)
        risk_score = compute_risk_score(probabilities)

        device = self._upsert_device(session, telemetry, now)

        reading = Reading(
            device_id=device.id,
            recorded_at=now,
            temperature_c=telemetry.temperature_c,
            humidity_pct=telemetry.humidity_pct,
            smoke_ppm=telemetry.smoke_ppm,
            air_quality_ppm=telemetry.air_quality_ppm,
            flame_analog_volts=telemetry.flame_analog_volts,
            flame_detected=telemetry.flame_detected,
            temp_rate_c_per_min=features["temp_rate_c_per_min"],
            smoke_rate_ppm_per_min=features["smoke_rate_ppm_per_min"],
            heat_index_c=features["heat_index_c"],
            device_status=telemetry.device_status,
            server_status=status,
            fire_probability=round(fire_probability, 6),
            risk_score=risk_score,
        )
        session.add(reading)
        session.flush()

        alert = self._evaluate_alert(session, device, reading, state, status)

        payload = _reading_to_dict(reading)
        self._emit("reading", payload)
        if alert is not None:
            self._emit("alert", _alert_to_dict(alert))

        # Mirror to ThingSpeak last: a cloud outage must never block ingest.
        try:
            get_mirror().submit(payload)
        except Exception:
            logger.exception("ThingSpeak mirror raised; continuing")

        return payload

    # -- helpers -----------------------------------------------------------

    def _upsert_device(
        self, session: Session, telemetry: TelemetryIn, now: datetime
    ) -> Device:
        device = session.get(Device, telemetry.device_id)
        if device is None:
            device = Device(
                id=telemetry.device_id,
                name=telemetry.device_id,
                location=telemetry.location,
                firmware_version=telemetry.firmware_version,
                first_seen=now,
                last_seen=now,
                online=True,
            )
            session.add(device)
            session.flush()
            self._emit("device", _device_to_dict(device))
        else:
            was_offline = not device.online
            device.last_seen = now
            device.online = True
            if telemetry.location:
                device.location = telemetry.location
            if telemetry.firmware_version:
                device.firmware_version = telemetry.firmware_version
            if was_offline:
                self._emit("device", _device_to_dict(device))
        return device

    def _load_thresholds(self, session: Session) -> dict[str, float]:
        return {row.key: row.value for row in session.scalars(select(Threshold)).all()}

    def _evaluate_alert(
        self,
        session: Session,
        device: Device,
        reading: Reading,
        state: _DeviceState,
        status: str,
    ) -> Alert | None:
        """Decide whether this reading raises or resolves an alert.

        Alerts are debounced: a status must persist for N consecutive readings
        before it raises. A single anomalous sample should not page anyone, but
        N is kept small (2 for FIRE) so a real fire is not delayed.
        """
        settings = get_settings()
        thresholds = self._load_thresholds(session)

        with self._lock:
            state.recent_statuses.append(status)
            for name in ("SAFE", "WARNING", "FIRE"):
                if name == status:
                    state.consecutive[name] += 1
                else:
                    state.consecutive[name] = 0
            fire_streak = state.consecutive["FIRE"]
            warning_streak = state.consecutive["WARNING"]
            safe_streak = state.consecutive["SAFE"]

        open_alert = session.scalars(
            select(Alert)
            .where(Alert.device_id == device.id, Alert.resolved_at.is_(None))
            .order_by(Alert.triggered_at.desc())
        ).first()

        # Resolve: two consecutive SAFE readings clear an open alert.
        if status == "SAFE" and safe_streak >= 2:
            if open_alert is not None:
                open_alert.resolved_at = utcnow()
                session.flush()
                self._emit("alert", _alert_to_dict(open_alert))
            return None

        raise_fire = status == "FIRE" and fire_streak >= settings.fire_alert_consecutive
        raise_warning = (
            status == "WARNING" and warning_streak >= settings.warning_alert_consecutive
        )
        if not (raise_fire or raise_warning):
            return None

        severity = "FIRE" if raise_fire else "WARNING"

        # Do not spam: only raise if there is no open alert at >= this severity.
        if open_alert is not None and SEVERITY_RANK[open_alert.severity] >= SEVERITY_RANK[
            severity
        ]:
            return None

        # An escalation supersedes the lower-severity alert.
        if open_alert is not None:
            open_alert.resolved_at = utcnow()

        message = _alert_message(severity, reading, thresholds)
        alert = Alert(
            device_id=device.id,
            severity=severity,
            message=message,
            triggered_at=utcnow(),
            temperature_c=reading.temperature_c,
            smoke_ppm=reading.smoke_ppm,
            fire_probability=reading.fire_probability,
        )
        session.add(alert)
        session.flush()
        logger.warning("ALERT [%s] %s: %s", severity, device.id, message)
        return alert

    def _emit(self, message_type: str, payload: dict[str, Any]) -> None:
        if self._broadcast is None:
            return
        try:
            self._broadcast(message_type, payload)
        except Exception:
            logger.exception("broadcast of %s failed", message_type)

    # -- offline sweep -----------------------------------------------------

    def mark_stale_devices_offline(self) -> list[str]:
        """Flip devices to offline when they stop reporting. Returns changed ids."""
        settings = get_settings()
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.device_offline_after_s
        )
        changed: list[str] = []
        with session_scope() as session:
            devices = session.scalars(
                select(Device).where(Device.online.is_(True))
            ).all()
            for device in devices:
                last_seen = device.last_seen
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                if last_seen < cutoff:
                    device.online = False
                    changed.append(device.id)
                    self._emit("device", _device_to_dict(device))
        if changed:
            logger.info("marked %d device(s) offline: %s", len(changed), changed)
        return changed


def _alert_message(
    severity: str, reading: Reading, thresholds: dict[str, float]
) -> str:
    reasons: list[str] = []
    if reading.temperature_c > thresholds.get("temperature_fire_c", 58.0):
        reasons.append(f"temperature {reading.temperature_c:.1f} C")
    elif reading.temperature_c > thresholds.get("temperature_warning_c", 45.0):
        reasons.append(f"elevated temperature {reading.temperature_c:.1f} C")
    if reading.smoke_ppm > thresholds.get("smoke_fire_ppm", 2200.0):
        reasons.append(f"heavy smoke {reading.smoke_ppm:.0f} ppm")
    elif reading.smoke_ppm > thresholds.get("smoke_warning_ppm", 900.0):
        reasons.append(f"smoke {reading.smoke_ppm:.0f} ppm")
    if reading.flame_analog_volts > thresholds.get("flame_volts", 1.15):
        reasons.append("flame detected")
    if reading.air_quality_ppm > thresholds.get("air_quality_warning_ppm", 300.0):
        reasons.append(f"air quality {reading.air_quality_ppm:.0f} ppm")
    if reading.temp_rate_c_per_min > 5.0:
        reasons.append(f"rising {reading.temp_rate_c_per_min:.1f} C/min")

    detail = ", ".join(reasons) if reasons else "model classification"
    prefix = "Fire detected" if severity == "FIRE" else "Fire risk warning"
    return f"{prefix}: {detail} (P(fire)={reading.fire_probability:.2f})"


def _reading_to_dict(reading: Reading) -> dict[str, Any]:
    return {
        "id": reading.id,
        "device_id": reading.device_id,
        "recorded_at": reading.recorded_at.isoformat(),
        "temperature_c": reading.temperature_c,
        "humidity_pct": reading.humidity_pct,
        "smoke_ppm": reading.smoke_ppm,
        "air_quality_ppm": reading.air_quality_ppm,
        "flame_analog_volts": reading.flame_analog_volts,
        "flame_detected": reading.flame_detected,
        "temp_rate_c_per_min": reading.temp_rate_c_per_min,
        "smoke_rate_ppm_per_min": reading.smoke_rate_ppm_per_min,
        "heat_index_c": reading.heat_index_c,
        "device_status": reading.device_status,
        "server_status": reading.server_status,
        "fire_probability": reading.fire_probability,
        "risk_score": reading.risk_score,
    }


def _alert_to_dict(alert: Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "device_id": alert.device_id,
        "severity": alert.severity,
        "message": alert.message,
        "triggered_at": alert.triggered_at.isoformat(),
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "acknowledged": alert.acknowledged,
        "temperature_c": alert.temperature_c,
        "smoke_ppm": alert.smoke_ppm,
        "fire_probability": alert.fire_probability,
    }


def _device_to_dict(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "location": device.location,
        "firmware_version": device.firmware_version,
        "first_seen": device.first_seen.isoformat(),
        "last_seen": device.last_seen.isoformat(),
        "online": device.online,
    }


_service: IngestService | None = None


def get_ingest_service() -> IngestService:
    global _service
    if _service is None:
        _service = IngestService()
    return _service


def reset_ingest_service() -> None:
    global _service
    _service = None
