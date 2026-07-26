"""Pydantic models for the MQTT payloads and the REST/WebSocket API."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FireStatus = Literal["SAFE", "WARNING", "FIRE"]

#: A camera node and an ESP32 node measure different physical quantities and
#: are classified by different models. Every reading carries its kind so no
#: consumer can read one as the other.
SensorKind = Literal["hardware", "camera"]

#: Physically possible envelopes. Anything outside these is a sensor fault, not
#: a fire, and is rejected at ingest rather than being fed to the model.
TEMPERATURE_RANGE = (-40.0, 300.0)
HUMIDITY_RANGE = (0.0, 100.0)
SMOKE_RANGE = (0.0, 100_000.0)
AIR_QUALITY_RANGE = (0.0, 100_000.0)
FLAME_VOLTS_RANGE = (0.0, 3.3)


def _reject_non_finite(value: float, field: str) -> float:
    """NaN and infinity poison every downstream aggregate; reject at the door.

    A failed DHT22 read returns NaN on real hardware, so this path is exercised
    in production, not just by malformed payloads.
    """
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite, got {value!r}")
    return value


class TelemetryIn(BaseModel):
    """A telemetry message published by a device on ``<prefix>/<id>/telemetry``."""

    model_config = ConfigDict(extra="ignore")

    device_id: Annotated[str, Field(min_length=1, max_length=64)]
    temperature_c: float
    humidity_pct: float
    smoke_ppm: float
    air_quality_ppm: float
    flame_analog_volts: float
    flame_detected: Annotated[int, Field(ge=0, le=1)] = 0
    #: The on-device decision tree's verdict, if the device sent one.
    device_status: FireStatus = "SAFE"
    firmware_version: str = ""
    location: str = ""
    #: Device-side timestamp; ignored for ordering (clocks drift) but retained.
    device_uptime_s: float = 0.0

    @field_validator(
        "temperature_c",
        "humidity_pct",
        "smoke_ppm",
        "air_quality_ppm",
        "flame_analog_volts",
        "device_uptime_s",
    )
    @classmethod
    def _finite(cls, value: float, info: object) -> float:
        name = getattr(info, "field_name", "value")
        return _reject_non_finite(value, str(name))

    @field_validator("temperature_c")
    @classmethod
    def _temp_range(cls, value: float) -> float:
        low, high = TEMPERATURE_RANGE
        if not low <= value <= high:
            raise ValueError(f"temperature_c {value} outside [{low}, {high}]")
        return value

    @field_validator("humidity_pct")
    @classmethod
    def _humidity_range(cls, value: float) -> float:
        low, high = HUMIDITY_RANGE
        if not low <= value <= high:
            raise ValueError(f"humidity_pct {value} outside [{low}, {high}]")
        return value

    @field_validator("smoke_ppm")
    @classmethod
    def _smoke_range(cls, value: float) -> float:
        low, high = SMOKE_RANGE
        if not low <= value <= high:
            raise ValueError(f"smoke_ppm {value} outside [{low}, {high}]")
        return value

    @field_validator("air_quality_ppm")
    @classmethod
    def _air_range(cls, value: float) -> float:
        low, high = AIR_QUALITY_RANGE
        if not low <= value <= high:
            raise ValueError(f"air_quality_ppm {value} outside [{low}, {high}]")
        return value

    @field_validator("flame_analog_volts")
    @classmethod
    def _flame_range(cls, value: float) -> float:
        low, high = FLAME_VOLTS_RANGE
        if not low <= value <= high:
            raise ValueError(f"flame_analog_volts {value} outside [{low}, {high}]")
        return value


class CameraTelemetryIn(BaseModel):
    """One analysed video frame from a browser camera node.

    Every metric is dimensionless and in [0, 1]; the browser does the pixel
    work and sends conclusions, not frames. No image data ever leaves the
    device — that is a privacy property, not an optimisation, and it is why
    this is three floats rather than a video upload.

    ``flicker`` is deliberately NOT accepted from the client. It is derived
    server-side from the history of ``flame_ratio`` (see ``app.camera``), so a
    client cannot claim a convincing flicker score for a static image.
    """

    model_config = ConfigDict(extra="ignore")

    device_id: Annotated[str, Field(min_length=1, max_length=64)]
    flame_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    luminance: Annotated[float, Field(ge=0.0, le=1.0)]
    haze_index: Annotated[float, Field(ge=0.0, le=1.0)]

    #: Genuine ambient conditions from the visitor's location, when they allow
    #: it. Optional and nullable: absent means "unknown", never "zero".
    temperature_c: float | None = None
    humidity_pct: float | None = None
    location: str = ""

    @field_validator("flame_ratio", "luminance", "haze_index")
    @classmethod
    def _finite_metric(cls, value: float, info: object) -> float:
        name = getattr(info, "field_name", "value")
        return _reject_non_finite(value, str(name))

    @field_validator("temperature_c")
    @classmethod
    def _optional_temp(cls, value: float | None) -> float | None:
        if value is None:
            return None
        _reject_non_finite(value, "temperature_c")
        low, high = TEMPERATURE_RANGE
        if not low <= value <= high:
            raise ValueError(f"temperature_c {value} outside [{low}, {high}]")
        return value

    @field_validator("humidity_pct")
    @classmethod
    def _optional_humidity(cls, value: float | None) -> float | None:
        if value is None:
            return None
        _reject_non_finite(value, "humidity_pct")
        low, high = HUMIDITY_RANGE
        if not low <= value <= high:
            raise ValueError(f"humidity_pct {value} outside [{low}, {high}]")
        return value


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    recorded_at: datetime
    #: Which sensor family produced this row. Consumers must branch on it
    #: before interpreting any channel below.
    sensor_kind: SensorKind = "hardware"

    # Gas / thermal. None on a camera node, which cannot measure them.
    temperature_c: float | None = None
    humidity_pct: float | None = None
    smoke_ppm: float | None = None
    air_quality_ppm: float | None = None
    flame_analog_volts: float | None = None
    flame_detected: int = 0

    # Camera. None on a hardware node.
    flame_ratio: float | None = None
    luminance: float | None = None
    haze_index: float | None = None
    flicker: float | None = None

    temp_rate_c_per_min: float = 0.0
    smoke_rate_ppm_per_min: float = 0.0
    heat_index_c: float = 0.0
    device_status: FireStatus
    server_status: FireStatus
    fire_probability: float
    risk_score: float


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    location: str
    kind: SensorKind = "hardware"
    firmware_version: str
    first_seen: datetime
    last_seen: datetime
    online: bool


class DeviceDetail(DeviceOut):
    latest_reading: ReadingOut | None = None
    open_alerts: int = 0


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    severity: FireStatus
    message: str
    triggered_at: datetime
    resolved_at: datetime | None
    acknowledged: bool
    temperature_c: float
    smoke_ppm: float
    fire_probability: float


class ThresholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value: float
    updated_at: datetime


class ThresholdUpdate(BaseModel):
    """A single threshold update. Values must be finite and non-negative."""

    value: Annotated[float, Field(ge=0.0)]

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        return _reject_non_finite(value, "value")


class ThresholdBulkUpdate(BaseModel):
    thresholds: dict[str, float]

    @field_validator("thresholds")
    @classmethod
    def _validate(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("at least one threshold required")
        for key, item in value.items():
            _reject_non_finite(item, key)
            if item < 0:
                raise ValueError(f"{key} must be non-negative")
        return value


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    #: 8 is the floor, not a recommendation. Length beats complexity rules,
    #: which mostly teach people to end passwords with "1!".
    password: Annotated[str, Field(min_length=8, max_length=128)]
    display_name: Annotated[str, Field(max_length=128)] = ""

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        email = value.strip().lower()
        # Not a full RFC 5322 validator on purpose: those reject valid
        # addresses and accept invalid ones. Deliverability is proven by mail
        # arriving, not by a regex.
        local, _, domain = email.partition("@")
        if not local or not domain or "." not in domain or " " in email:
            raise ValueError("enter a valid email address")
        return email


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    created_at: datetime
    emergency_email: str
    emergency_name: str
    temperature_limit_c: float
    notifications_enabled: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


class EmergencyContactUpdate(BaseModel):
    """Partial update. Unset fields are left alone."""

    emergency_email: Annotated[str, Field(max_length=320)] | None = None
    emergency_name: Annotated[str, Field(max_length=128)] | None = None
    #: Bounded to what a room can plausibly reach. Below 30 C the alarm would
    #: fire on a warm afternoon; above 150 C the sensor is already destroyed
    #: and waiting for it would mean never alerting at all.
    temperature_limit_c: Annotated[float, Field(ge=30.0, le=150.0)] | None = None
    notifications_enabled: bool | None = None

    @field_validator("emergency_email")
    @classmethod
    def _contact_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        email = value.strip().lower()
        if not email:
            return ""  # explicit clear: notifications off
        local, _, domain = email.partition("@")
        if not local or not domain or "." not in domain or " " in email:
            raise ValueError("enter a valid email address")
        return email


class TemperatureEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    started_at: datetime
    ended_at: datetime | None
    trigger_temperature_c: float
    peak_temperature_c: float
    threshold_c: float
    sensor_kind: SensorKind
    notified: bool
    notify_error: str


class ClaimDeviceRequest(BaseModel):
    device_id: Annotated[str, Field(min_length=1, max_length=64)]


class NotificationStatusOut(BaseModel):
    """So the settings page can say whether email would actually work."""

    transport: Literal["resend", "smtp", "none"]
    configured: bool
    contact_set: bool


class StatsOut(BaseModel):
    total_devices: int
    online_devices: int
    total_readings: int
    open_alerts: int
    alerts_24h: int
    current_status: FireStatus
    max_fire_probability: float


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    mqtt_connected: bool
    model_loaded: bool
    thingspeak_pending: int
    version: str


class PredictRequest(BaseModel):
    """Ad-hoc classification request, used by tests and the API playground."""

    temperature_c: float
    humidity_pct: float
    smoke_ppm: float
    air_quality_ppm: float
    flame_analog_volts: float
    flame_detected: Annotated[int, Field(ge=0, le=1)] = 0
    temp_rate_c_per_min: float = 0.0
    smoke_rate_ppm_per_min: float = 0.0


class PredictResponse(BaseModel):
    status: FireStatus
    fire_probability: float
    risk_score: float
    probabilities: dict[str, float]


class WSMessage(BaseModel):
    """Envelope for every WebSocket broadcast."""

    type: Literal["reading", "alert", "device", "stats", "hello"]
    payload: dict[str, object]
