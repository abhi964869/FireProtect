"""Pydantic models for the MQTT payloads and the REST/WebSocket API."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FireStatus = Literal["SAFE", "WARNING", "FIRE"]

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


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    recorded_at: datetime
    temperature_c: float
    humidity_pct: float
    smoke_ppm: float
    air_quality_ppm: float
    flame_analog_volts: float
    flame_detected: int
    temp_rate_c_per_min: float
    smoke_rate_ppm_per_min: float
    heat_index_c: float
    device_status: FireStatus
    server_status: FireStatus
    fire_probability: float
    risk_score: float


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    location: str
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
