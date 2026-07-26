"""The ingest pipeline: telemetry in, persisted reading + alerts + broadcast out.

This is the single place where a telemetry message becomes state. Both the MQTT
subscriber and the REST ingest endpoint route through ``process_telemetry`` so
the two paths cannot drift apart.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.camera import describe, get_camera_classifier
from app.config import get_settings
from app.db import (
    Alert,
    Device,
    Reading,
    TemperatureEvent,
    Threshold,
    session_scope,
    utcnow,
)
from app.inference import compute_heat_index, compute_risk_score, get_classifier
from app.notify import EmergencyMessage, get_notifier
from app.schemas import CameraTelemetryIn, TelemetryIn
from app.thingspeak import get_mirror

logger = logging.getLogger(__name__)

#: Severity ordering, used to decide whether a state change is an escalation.
SEVERITY_RANK = {"SAFE": 0, "WARNING": 1, "FIRE": 2}

#: How many times the email thread re-looks-up its event row before giving up.
#: Covers the window between the row being flushed and its transaction being
#: committed; a handful of short waits is ample for a local commit.
_EMAIL_RESULT_RETRIES = 6
_EMAIL_RESULT_BACKOFF_S = 0.05


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

        device = self._upsert_device(
            session,
            device_id=telemetry.device_id,
            location=telemetry.location,
            firmware_version=telemetry.firmware_version,
            kind="hardware",
            now=now,
        )

        reading = Reading(
            device_id=device.id,
            recorded_at=now,
            sensor_kind="hardware",
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
        self._check_temperature_emergency(session, device, reading)

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

    # -- emergency escalation ---------------------------------------------

    def _check_temperature_emergency(
        self, session: Session, device: Device, reading: Reading
    ) -> None:
        """Open or extend a temperature excursion, and mail the contact once.

        Runs on every reading that carries a temperature. Deliberately separate
        from the alert engine: an alert is a dashboard-level judgement made by
        a model, while this is a hard physical threshold the account holder set
        themselves. Someone who asked to be emailed at 55 C should be emailed
        at 55 C whether or not a random forest agrees it is a fire.
        """
        temperature = reading.temperature_c
        if temperature is None:
            return

        owner = device.owner
        settings = get_settings()

        if owner is not None:
            if not owner.notifications_enabled:
                return
            limit = owner.temperature_limit_c
            recipient = owner.emergency_email
            recipient_name = owner.emergency_name
        else:
            # No account owns this node. Only a deployment-wide fallback can
            # legitimately be notified.
            limit = settings.fallback_temperature_limit_c
            recipient = settings.fallback_emergency_email
            recipient_name = ""

        # An excursion stays open until temperature drops back under the limit,
        # so a fire that burns for ten minutes is one row, not three hundred.
        open_event = session.scalars(
            select(TemperatureEvent)
            .where(
                TemperatureEvent.device_id == device.id,
                TemperatureEvent.ended_at.is_(None),
            )
            .order_by(TemperatureEvent.started_at.desc())
        ).first()

        if temperature < limit:
            if open_event is not None:
                open_event.ended_at = utcnow()
                session.flush()
            return

        if open_event is not None:
            if temperature > open_event.peak_temperature_c:
                open_event.peak_temperature_c = temperature
                session.flush()
            return

        if owner is None and not recipient:
            # Nothing to record against and nobody to tell.
            return

        event = TemperatureEvent(
            user_id=owner.id if owner is not None else None,
            device_id=device.id,
            started_at=utcnow(),
            trigger_temperature_c=temperature,
            peak_temperature_c=temperature,
            threshold_c=limit,
            sensor_kind=reading.sensor_kind,
        )
        session.add(event)
        session.flush()

        if not recipient:
            return

        # Cooldown, so a flapping sensor cannot mail someone every two seconds.
        if owner is not None and owner.last_notified_at is not None:
            last = owner.last_notified_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (utcnow() - last).total_seconds() < settings.emergency_cooldown_s:
                logger.info(
                    "emergency email for %s suppressed by cooldown", device.id
                )
                return

        message = EmergencyMessage(
            to_email=recipient,
            to_name=recipient_name,
            device_id=device.id,
            location=device.location,
            temperature_c=temperature,
            threshold_c=limit,
            sensor_kind=reading.sensor_kind,
            occurred_at=event.started_at,
            dashboard_url=settings.public_url,
        )
        if owner is not None:
            owner.last_notified_at = utcnow()
        session.flush()
        self._dispatch_email(message, event, session)

    def _dispatch_email(
        self, message: EmergencyMessage, event: TemperatureEvent, session: Session
    ) -> None:
        """Send on a worker thread and record the outcome.

        Never on the calling thread: SMTP can block for the full socket
        timeout, and the reading that triggered this still has to reach the
        database and the dashboard. A fire alert that waits on Gmail is a fire
        alert that might never arrive.
        """
        notifier = get_notifier()
        event_id = event.id

        if not notifier.configured:
            logger.warning(
                "temperature emergency on event %s but no email transport is "
                "configured; set RESEND_API_KEY or SMTP_* to enable alerts",
                event_id,
            )
            # Written through the *caller's* session, not a new one. The event
            # row is not committed yet, so a second connection cannot see it —
            # this is what made the "no transport" case silently record
            # nothing at all.
            event.notified = False
            event.notify_error = "no email transport configured"
            session.flush()
            return

        def _run() -> None:
            try:
                notifier.send(message)
            except Exception as exc:
                logger.exception("emergency email failed")
                self._record_email_result(event_id, False, str(exc)[:500])
            else:
                self._record_email_result(event_id, True, "")

        threading.Thread(
            target=_run, name=f"fireprotect-email-{event_id}", daemon=True
        ).start()

    def _record_email_result(self, event_id: int, sent: bool, error: str) -> None:
        """Write the send outcome back, in its own transaction.

        Its own session because this runs on the email thread, after the ingest
        transaction has committed — usually. A fast transport (a stubbed one in
        tests, or Resend on a warm connection) can finish before the request
        that created the row commits, and the row would then be invisible here
        and the outcome lost. Hence the short retry: the ingest transaction is
        milliseconds from committing, so waiting briefly is correct where
        giving up immediately would drop the audit trail exactly when the
        email succeeded fastest.

        Failures here are logged and dropped. Losing one audit record must not
        take down ingest.
        """
        for attempt in range(_EMAIL_RESULT_RETRIES):
            try:
                with session_scope() as session:
                    event = session.get(TemperatureEvent, event_id)
                    if event is not None:
                        event.notified = sent
                        event.notify_error = error
                        return
            except Exception:
                logger.exception("could not record email result for event %s", event_id)
                return
            time.sleep(_EMAIL_RESULT_BACKOFF_S * (attempt + 1))
        logger.warning("event %s vanished before its email result was recorded", event_id)

    # -- camera nodes ------------------------------------------------------

    def process_camera_telemetry(
        self, telemetry: CameraTelemetryIn, session: Session | None = None
    ) -> dict[str, Any]:
        """Persist one analysed camera frame.

        Shares this class's device registry, alert debouncing, WebSocket
        broadcasting and offline sweep with the hardware path — those concerns
        are identical for any sensor. Only the *classification* differs, and
        that difference is isolated in ``app.camera``.
        """
        if session is not None:
            return self._process_camera(telemetry, session)
        with session_scope() as own_session:
            return self._process_camera(telemetry, own_session)

    def _process_camera(
        self, telemetry: CameraTelemetryIn, session: Session
    ) -> dict[str, Any]:
        now = utcnow()

        status, probabilities, evidence = get_camera_classifier().classify(
            device_id=telemetry.device_id,
            flame_ratio=telemetry.flame_ratio,
            luminance=telemetry.luminance,
            haze_index=telemetry.haze_index,
        )
        fire_probability = probabilities.get("FIRE", 0.0)
        risk_score = compute_risk_score(probabilities)

        device = self._upsert_device(
            session,
            device_id=telemetry.device_id,
            location=telemetry.location,
            firmware_version="",
            kind="camera",
            now=now,
        )

        with self._lock:
            state = self._states.setdefault(telemetry.device_id, _DeviceState())

        reading = Reading(
            device_id=device.id,
            recorded_at=now,
            sensor_kind="camera",
            # Gas and flame-diode channels stay NULL: this device has no such
            # sensors, and a zero here would be indistinguishable from a real
            # clean-air reading downstream.
            temperature_c=telemetry.temperature_c,
            humidity_pct=telemetry.humidity_pct,
            smoke_ppm=None,
            air_quality_ppm=None,
            flame_analog_volts=None,
            flame_detected=1 if status == "FIRE" else 0,
            flame_ratio=evidence.flame_ratio,
            luminance=evidence.luminance,
            haze_index=evidence.haze_index,
            flicker=evidence.flicker,
            heat_index_c=(
                round(
                    compute_heat_index(telemetry.temperature_c, telemetry.humidity_pct),
                    3,
                )
                if telemetry.temperature_c is not None
                and telemetry.humidity_pct is not None
                else 0.0
            ),
            device_status=status,
            server_status=status,
            fire_probability=round(fire_probability, 6),
            risk_score=risk_score,
        )
        session.add(reading)
        session.flush()

        alert = self._evaluate_alert(
            session, device, reading, state, status, message=describe(status, evidence)
        )
        # A camera node only has a temperature when the visitor shared their
        # location and the weather lookup succeeded; the check no-ops otherwise.
        self._check_temperature_emergency(session, device, reading)

        payload = _reading_to_dict(reading)
        payload["camera_evidence"] = evidence.as_dict()
        self._emit("reading", payload)
        if alert is not None:
            self._emit("alert", _alert_to_dict(alert))

        # Not mirrored to ThingSpeak: its channel fields are defined as the
        # eight hardware sensor values, and posting camera metrics into
        # field1="smoke_ppm" would corrupt the historical series.
        return payload

    # -- helpers -----------------------------------------------------------

    def _upsert_device(
        self,
        session: Session,
        *,
        device_id: str,
        location: str,
        firmware_version: str,
        kind: str,
        now: datetime,
    ) -> Device:
        device = session.get(Device, device_id)
        if device is None:
            device = Device(
                id=device_id,
                name=device_id,
                location=location,
                kind=kind,
                firmware_version=firmware_version,
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
            if location:
                device.location = location
            if firmware_version:
                device.firmware_version = firmware_version
            # A device that starts reporting as a different kind has been
            # repurposed (or an id was reused). Trust the live traffic over the
            # stale row rather than silently mislabelling every new reading.
            if device.kind != kind:
                logger.info(
                    "device %s changed kind %s -> %s", device_id, device.kind, kind
                )
                device.kind = kind
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
        message: str | None = None,
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

        # A camera node supplies its own wording, because "smoke 0 ppm" would
        # be a nonsense explanation for an alarm raised from video.
        text = message or _alert_message(severity, reading, thresholds)
        alert = Alert(
            device_id=device.id,
            severity=severity,
            message=text,
            triggered_at=utcnow(),
            temperature_c=reading.temperature_c or 0.0,
            smoke_ppm=reading.smoke_ppm or 0.0,
            fire_probability=reading.fire_probability,
        )
        session.add(alert)
        session.flush()
        logger.warning("ALERT [%s] %s: %s", severity, device.id, text)
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
    # Channels are nullable since camera nodes exist; a missing channel simply
    # contributes no reason rather than comparing None to a float.
    temperature = reading.temperature_c
    smoke = reading.smoke_ppm
    flame_volts = reading.flame_analog_volts
    air = reading.air_quality_ppm

    if temperature is not None:
        if temperature > thresholds.get("temperature_fire_c", 58.0):
            reasons.append(f"temperature {temperature:.1f} C")
        elif temperature > thresholds.get("temperature_warning_c", 45.0):
            reasons.append(f"elevated temperature {temperature:.1f} C")
    if smoke is not None:
        if smoke > thresholds.get("smoke_fire_ppm", 2200.0):
            reasons.append(f"heavy smoke {smoke:.0f} ppm")
        elif smoke > thresholds.get("smoke_warning_ppm", 900.0):
            reasons.append(f"smoke {smoke:.0f} ppm")
    if flame_volts is not None and flame_volts > thresholds.get("flame_volts", 1.15):
        reasons.append("flame detected")
    if air is not None and air > thresholds.get("air_quality_warning_ppm", 300.0):
        reasons.append(f"air quality {air:.0f} ppm")
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
        "sensor_kind": reading.sensor_kind,
        "temperature_c": reading.temperature_c,
        "humidity_pct": reading.humidity_pct,
        "smoke_ppm": reading.smoke_ppm,
        "air_quality_ppm": reading.air_quality_ppm,
        "flame_analog_volts": reading.flame_analog_volts,
        "flame_detected": reading.flame_detected,
        "flame_ratio": reading.flame_ratio,
        "luminance": reading.luminance,
        "haze_index": reading.haze_index,
        "flicker": reading.flicker,
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
        "kind": device.kind,
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
