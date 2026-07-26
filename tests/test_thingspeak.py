"""ThingSpeak mirror tests - buffering, retry and failure handling.

No live API key is required: the HTTP transport is substituted with fakes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.thingspeak import FIELD_MAP, ThingSpeakMirror


class RecordingTransport:
    """Captures posts and returns a scripted sequence of status codes."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or []
        self.calls: list[dict] = []

    def post(self, url: str, data: dict, timeout: float) -> int:
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self.statuses:
            return self.statuses.pop(0)
        return 200


class ExplodingTransport:
    """Always raises, simulating a dead network."""

    def __init__(self) -> None:
        self.attempts = 0

    def post(self, url: str, data: dict, timeout: float) -> int:
        self.attempts += 1
        raise ConnectionError("network is down")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        thingspeak_enabled=True,
        thingspeak_write_api_key="TESTKEY123",
        thingspeak_buffer_path=tmp_path / "buffer.jsonl",
        thingspeak_min_interval_s=0.0,
        thingspeak_max_retries=3,
        thingspeak_buffer_max_entries=5,
    )


@pytest.fixture
def reading() -> dict:
    return {
        "temperature_c": 24.5,
        "humidity_pct": 41.0,
        "smoke_ppm": 130.0,
        "air_quality_ppm": 88.0,
        "flame_analog_volts": 0.04,
        "flame_detected": 0,
        "fire_probability": 0.01,
        "risk_score": 2.5,
    }


# --------------------------------------------------------------------------
# Payload construction
# --------------------------------------------------------------------------


def test_payload_maps_every_field(settings, reading):
    mirror = ThingSpeakMirror(settings, RecordingTransport())
    payload = mirror.build_payload(reading)
    assert payload["api_key"] == "TESTKEY123"
    for field, source in FIELD_MAP.items():
        assert payload[field] == reading[source]


def test_payload_uses_zero_for_missing_values(settings):
    mirror = ThingSpeakMirror(settings, RecordingTransport())
    payload = mirror.build_payload({})
    assert payload["field1"] == 0


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_successful_submit_sends_immediately(settings, reading):
    transport = RecordingTransport([200])
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is True
    assert len(transport.calls) == 1
    assert mirror.pending == 0
    assert mirror.stats["sent"] == 1


def test_disabled_mirror_does_nothing(reading, tmp_path):
    settings = Settings(
        thingspeak_enabled=False, thingspeak_buffer_path=tmp_path / "b.jsonl"
    )
    transport = RecordingTransport()
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is False
    assert transport.calls == []


def test_missing_api_key_buffers_without_sending(reading, tmp_path):
    settings = Settings(
        thingspeak_enabled=True,
        thingspeak_write_api_key=None,
        thingspeak_buffer_path=tmp_path / "b.jsonl",
    )
    transport = RecordingTransport()
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is False
    assert transport.calls == []
    assert mirror.pending == 1


# --------------------------------------------------------------------------
# Offline buffering and retry
# --------------------------------------------------------------------------


def test_network_failure_buffers_the_reading(settings, reading):
    transport = ExplodingTransport()
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is False
    assert mirror.pending == 1
    # Retried up to the configured limit before giving up.
    assert transport.attempts == settings.thingspeak_max_retries


def test_buffer_drains_after_recovery(settings, reading):
    failing = ExplodingTransport()
    mirror = ThingSpeakMirror(settings, failing)
    mirror.submit(reading)
    mirror.submit({**reading, "temperature_c": 25.5})
    assert mirror.pending == 2

    mirror._transport = RecordingTransport([200, 200])
    assert mirror.flush() is True
    assert mirror.pending == 1  # one entry per flush call
    assert mirror.flush() is True
    assert mirror.pending == 0


def test_buffer_is_bounded_and_drops_oldest(settings, reading):
    mirror = ThingSpeakMirror(settings, ExplodingTransport())
    for index in range(10):
        mirror._enqueue({**reading, "temperature_c": float(index)})
    assert mirror.pending == settings.thingspeak_buffer_max_entries
    assert mirror.stats["dropped"] == 10 - settings.thingspeak_buffer_max_entries
    # The newest entries survive; the oldest are discarded.
    assert mirror._buffer[-1]["temperature_c"] == 9.0


def test_buffer_persists_across_restart(settings, reading):
    mirror = ThingSpeakMirror(settings, ExplodingTransport())
    mirror.submit(reading)
    assert Path(settings.thingspeak_buffer_path).exists()

    revived = ThingSpeakMirror(settings, RecordingTransport([200]))
    assert revived.pending == 1
    assert revived.flush() is True


def test_corrupt_buffer_line_is_skipped(settings, reading):
    path = Path(settings.thingspeak_buffer_path)
    path.write_text(
        json.dumps(reading) + "\n{ this is not valid json\n", encoding="utf-8"
    )
    mirror = ThingSpeakMirror(settings, RecordingTransport())
    assert mirror.pending == 1


def test_missing_buffer_file_is_fine(settings):
    assert ThingSpeakMirror(settings, RecordingTransport()).pending == 0


# --------------------------------------------------------------------------
# HTTP status handling
# --------------------------------------------------------------------------


def test_server_error_is_retried(settings, reading):
    transport = RecordingTransport([500, 500, 200])
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is True
    assert len(transport.calls) == 3


def test_rate_limit_is_retried(settings, reading):
    transport = RecordingTransport([429, 200])
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is True
    assert len(transport.calls) == 2


def test_client_error_is_not_retried_and_drops_entry(settings, reading):
    """A 401 will never succeed on retry; retrying it just wastes time."""
    transport = RecordingTransport([401])
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is False
    assert len(transport.calls) == 1
    assert mirror.pending == 0
    assert mirror.stats["dropped"] == 1


def test_persistent_failure_records_consecutive_failures(settings, reading):
    mirror = ThingSpeakMirror(settings, ExplodingTransport())
    mirror.submit(reading)
    assert mirror.stats["consecutive_failures"] >= 1


def test_rate_limit_interval_buffers_instead_of_sending(reading, tmp_path):
    settings = Settings(
        thingspeak_enabled=True,
        thingspeak_write_api_key="K",
        thingspeak_buffer_path=tmp_path / "b.jsonl",
        thingspeak_min_interval_s=999.0,
    )
    transport = RecordingTransport([200])
    mirror = ThingSpeakMirror(settings, transport)
    assert mirror.submit(reading) is True  # first send is allowed
    assert mirror.submit(reading) is False  # second is inside the interval
    assert mirror.pending == 1
