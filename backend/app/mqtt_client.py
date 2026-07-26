"""MQTT subscriber.

Runs paho-mqtt's network loop on its own thread, decodes telemetry, and hands
each message to the ingest service. Every callback is defensive: a malformed
payload from one device must never take down ingest for the others.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.ingest import get_ingest_service
from app.schemas import TelemetryIn

logger = logging.getLogger(__name__)


class MqttSubscriber:
    """Thin wrapper around ``paho.mqtt.client.Client`` with reconnect handling."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None
        self._connected = False
        self._messages_received = 0
        self._messages_rejected = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict[str, int | bool]:
        return {
            "connected": self._connected,
            "received": self._messages_received,
            "rejected": self._messages_rejected,
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Connect and start the background network loop.

        Returns False if the broker is unreachable. This is deliberately not
        fatal: the API and dashboard must still come up so the operator can see
        *that* the broker is down, which is exactly when they need the UI.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError:  # pragma: no cover
            logger.error("paho-mqtt is not installed; MQTT ingest disabled")
            return False

        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._settings.mqtt_client_id,
                clean_session=True,
            )
        except AttributeError:  # pragma: no cover - paho < 2.0
            client = mqtt.Client(client_id=self._settings.mqtt_client_id)

        if self._settings.mqtt_username:
            client.username_pw_set(
                self._settings.mqtt_username, self._settings.mqtt_password
            )

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        # Exponential reconnect backoff, so a broker outage does not turn into
        # a reconnect storm against a broker that is already struggling.
        client.reconnect_delay_set(
            min_delay=int(self._settings.mqtt_reconnect_min_s),
            max_delay=int(self._settings.mqtt_reconnect_max_s),
        )

        self._client = client
        try:
            client.connect(
                self._settings.mqtt_host,
                self._settings.mqtt_port,
                self._settings.mqtt_keepalive,
            )
        except OSError as exc:
            logger.warning(
                "MQTT broker %s:%s unreachable (%s); the API will run without "
                "live ingest and paho will keep retrying",
                self._settings.mqtt_host,
                self._settings.mqtt_port,
                exc,
            )
            client.loop_start()
            return False

        client.loop_start()
        logger.info(
            "MQTT connecting to %s:%s",
            self._settings.mqtt_host,
            self._settings.mqtt_port,
        )
        return True

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # pragma: no cover
            logger.exception("error during MQTT shutdown")
        finally:
            self._connected = False
            self._client = None

    # -- callbacks ---------------------------------------------------------

    def _on_connect(
        self,
        client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        code = int(getattr(reason_code, "value", reason_code) or 0)
        if code != 0:
            self._connected = False
            logger.error("MQTT connection refused with code %s", code)
            return
        self._connected = True
        for topic in (
            self._settings.telemetry_topic,
            self._settings.alert_topic,
            self._settings.status_topic,
        ):
            client.subscribe(topic, qos=1)
            logger.info("subscribed to %s", topic)

    def _on_disconnect(
        self,
        _client: Any,
        _userdata: Any,
        *args: Any,
        **_kwargs: Any,
    ) -> None:
        self._connected = False
        logger.warning("MQTT disconnected (%s); paho will retry with backoff", args)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        topic = getattr(message, "topic", "")
        try:
            self.handle_message(topic, message.payload)
        except Exception:
            # Never let an exception escape into paho's loop thread.
            logger.exception("unhandled error processing message on %s", topic)

    # -- message handling (public for tests) -------------------------------

    def handle_message(self, topic: str, raw: bytes | str) -> bool:
        """Decode and ingest one message. Returns True if it was accepted."""
        self._messages_received += 1

        parts = topic.split("/")
        if len(parts) < 3:
            self._messages_rejected += 1
            logger.warning("ignoring message on malformed topic %r", topic)
            return False
        device_id, kind = parts[1], parts[2]

        if kind != "telemetry":
            # Alert and status topics are informational; devices are not the
            # source of truth for alerts, the server-side model is.
            logger.debug("ignoring non-telemetry message on %s", topic)
            return False

        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._messages_rejected += 1
            logger.warning("undecodable payload on %s: %s", topic, exc)
            return False

        if not isinstance(data, dict):
            self._messages_rejected += 1
            logger.warning("payload on %s is %s, expected object", topic, type(data))
            return False

        # The topic is authoritative for device identity - a device cannot
        # impersonate another by putting a different id in the body.
        data["device_id"] = device_id

        try:
            telemetry = TelemetryIn.model_validate(data)
        except ValidationError as exc:
            self._messages_rejected += 1
            logger.warning(
                "rejected telemetry from %s: %s",
                device_id,
                "; ".join(f"{e['loc']}: {e['msg']}" for e in exc.errors()),
            )
            return False

        try:
            get_ingest_service().process_telemetry(telemetry)
        except Exception:
            self._messages_rejected += 1
            logger.exception("failed to persist telemetry from %s", device_id)
            return False
        return True


_subscriber: MqttSubscriber | None = None


def get_subscriber() -> MqttSubscriber:
    global _subscriber
    if _subscriber is None:
        _subscriber = MqttSubscriber()
    return _subscriber


def reset_subscriber() -> None:
    global _subscriber
    _subscriber = None
