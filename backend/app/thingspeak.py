"""ThingSpeak cloud mirror with offline buffering and bounded retry.

Every reading accepted by the backend is mirrored to a ThingSpeak channel. The
network is assumed to be unreliable, so:

* failures append to a newline-delimited JSON buffer on disk;
* the buffer is drained on the next successful send;
* the buffer is capped, dropping the *oldest* entries first (recent data is more
  useful during an incident than stale data);
* retries use exponential backoff and stop after ``thingspeak_max_retries``.

Sending is fully mockable — no live API key is needed for the test suite.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: ThingSpeak field mapping. Field numbers are fixed by the channel layout
#: documented in docs/SETUP.md; changing these silently corrupts the channel.
FIELD_MAP: dict[str, str] = {
    "field1": "temperature_c",
    "field2": "humidity_pct",
    "field3": "smoke_ppm",
    "field4": "air_quality_ppm",
    "field5": "flame_analog_volts",
    "field6": "flame_detected",
    "field7": "fire_probability",
    "field8": "risk_score",
}


class Transport(Protocol):
    """Minimal HTTP surface, so tests can substitute a fake."""

    def post(self, url: str, data: dict[str, Any], timeout: float) -> int:
        """Return the HTTP status code. Raise on transport failure."""


class HttpxTransport:
    """Real transport backed by httpx."""

    def post(self, url: str, data: dict[str, Any], timeout: float) -> int:
        import httpx

        response = httpx.post(url, data=data, timeout=timeout)
        return response.status_code


class ThingSpeakMirror:
    """Mirrors readings to ThingSpeak, buffering while offline."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport or HttpxTransport()
        self._lock = threading.RLock()
        self._buffer: deque[dict[str, Any]] = deque(
            maxlen=self._settings.thingspeak_buffer_max_entries
        )
        self._last_send_at = 0.0
        self._consecutive_failures = 0
        self._sent_count = 0
        self._dropped_count = 0
        self._buffer_path = Path(self._settings.thingspeak_buffer_path)
        self._load_buffer()

    # -- buffer persistence ------------------------------------------------

    def _load_buffer(self) -> None:
        """Restore anything left over from a previous run."""
        if not self._buffer_path.exists():
            return
        try:
            with self._buffer_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._buffer.append(json.loads(line))
                    except json.JSONDecodeError:
                        # A partially written final line is expected after an
                        # unclean shutdown; skip it rather than failing to boot.
                        logger.warning("skipping corrupt buffer line")
            logger.info("restored %d buffered ThingSpeak entries", len(self._buffer))
        except OSError:
            logger.exception("could not read ThingSpeak buffer at %s", self._buffer_path)

    def _persist_buffer(self) -> None:
        try:
            self._buffer_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._buffer_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for entry in self._buffer:
                    handle.write(json.dumps(entry) + "\n")
            tmp.replace(self._buffer_path)
        except OSError:
            logger.exception("could not persist ThingSpeak buffer")

    # -- public API --------------------------------------------------------

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending": len(self._buffer),
                "sent": self._sent_count,
                "dropped": self._dropped_count,
                "consecutive_failures": self._consecutive_failures,
            }

    def build_payload(self, reading: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "api_key": self._settings.thingspeak_write_api_key or ""
        }
        for field, source in FIELD_MAP.items():
            payload[field] = reading.get(source, 0)
        return payload

    def submit(self, reading: dict[str, Any]) -> bool:
        """Mirror one reading. Returns True if it reached ThingSpeak now.

        A False return is not an error: the entry is buffered and retried.
        """
        if not self._settings.thingspeak_enabled:
            return False
        if not self._settings.thingspeak_write_api_key:
            logger.debug("ThingSpeak enabled but no write API key; buffering only")
            self._enqueue(reading)
            return False

        with self._lock:
            # Respect the free tier's rate limit rather than being throttled.
            since_last = time.monotonic() - self._last_send_at
            if since_last < self._settings.thingspeak_min_interval_s:
                self._enqueue(reading)
                return False

        self._enqueue(reading)
        return self.flush()

    def _enqueue(self, reading: dict[str, Any]) -> None:
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                # deque drops the oldest automatically; count it so the drop is
                # visible in /health rather than silent.
                self._dropped_count += 1
            self._buffer.append(reading)

    def flush(self) -> bool:
        """Attempt to drain the buffer. Returns True if anything was sent."""
        if not self._settings.thingspeak_enabled:
            return False

        sent_any = False
        while True:
            with self._lock:
                if not self._buffer:
                    break
                entry = self._buffer[0]

            if not self._send_with_retry(entry):
                self._persist_buffer()
                return sent_any

            with self._lock:
                if self._buffer and self._buffer[0] is entry:
                    self._buffer.popleft()
                self._sent_count += 1
                self._last_send_at = time.monotonic()
                self._consecutive_failures = 0
            sent_any = True

            # One send per call keeps ingest latency bounded; the remainder of
            # the buffer drains on subsequent readings.
            break

        self._persist_buffer()
        return sent_any

    def _send_with_retry(self, reading: dict[str, Any]) -> bool:
        payload = self.build_payload(reading)
        delay = 0.5
        for attempt in range(1, self._settings.thingspeak_max_retries + 1):
            try:
                status = self._transport.post(
                    self._settings.thingspeak_url,
                    payload,
                    self._settings.thingspeak_timeout_s,
                )
            except Exception as exc:
                logger.warning(
                    "ThingSpeak send failed (attempt %d/%d): %s",
                    attempt,
                    self._settings.thingspeak_max_retries,
                    exc,
                )
            else:
                if 200 <= status < 300:
                    return True
                # 4xx other than 429 will never succeed on retry.
                if 400 <= status < 500 and status != 429:
                    logger.error(
                        "ThingSpeak rejected update with %d; dropping entry", status
                    )
                    with self._lock:
                        self._dropped_count += 1
                        if self._buffer and self._buffer[0] is reading:
                            self._buffer.popleft()
                    return False
                logger.warning(
                    "ThingSpeak returned %d (attempt %d/%d)",
                    status,
                    attempt,
                    self._settings.thingspeak_max_retries,
                )

            if attempt < self._settings.thingspeak_max_retries:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)

        with self._lock:
            self._consecutive_failures += 1
        return False


_mirror: ThingSpeakMirror | None = None


def get_mirror() -> ThingSpeakMirror:
    global _mirror
    if _mirror is None:
        _mirror = ThingSpeakMirror()
    return _mirror


def reset_mirror() -> None:
    """Drop the singleton. Used by tests."""
    global _mirror
    _mirror = None
