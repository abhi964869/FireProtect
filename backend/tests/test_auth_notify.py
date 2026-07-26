"""Accounts, device ownership, and emergency email escalation."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import auth, notify
from app.config import get_settings
from app.notify import EmailNotifier, EmergencyMessage

CREDENTIALS = {"email": "Owner@Example.COM", "password": "correct horse battery"}


def register(client: TestClient, **overrides: Any) -> dict:
    payload = {**CREDENTIALS, **overrides}
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_password_round_trip() -> None:
    digest = auth.hash_password("a decent passphrase")
    assert digest != "a decent passphrase"
    assert auth.verify_password("a decent passphrase", digest)
    assert not auth.verify_password("a decent passphrasf", digest)


def test_identical_passwords_hash_differently() -> None:
    """Per-password salt: two users with the same password must not collide."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_overlong_password_is_rejected_not_truncated() -> None:
    """bcrypt ignores everything past 72 bytes.

    Accepting the input would make two different long passwords interchangeable,
    which is worse than refusing it.
    """
    with pytest.raises(ValueError, match="72 bytes"):
        auth.hash_password("x" * 73)


def test_malformed_stored_hash_does_not_raise() -> None:
    assert auth.verify_password("anything", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_token_round_trip() -> None:
    token, expires_in = auth.create_token(42)
    assert expires_in > 0
    assert auth.decode_token(token) == 42


def test_tampered_token_is_rejected() -> None:
    token, _ = auth.create_token(42)
    forged = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert auth.decode_token(forged) is None


def test_token_signed_with_another_secret_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "secret-one")
    get_settings.cache_clear()
    token, _ = auth.create_token(7)

    monkeypatch.setenv("JWT_SECRET", "secret-two")
    get_settings.cache_clear()
    assert auth.decode_token(token) is None


def test_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_TTL_HOURS", "-1")
    get_settings.cache_clear()
    token, _ = auth.create_token(7)
    assert auth.decode_token(token) is None


def test_garbage_token_is_rejected() -> None:
    assert auth.decode_token("not.a.jwt") is None
    assert auth.decode_token("") is None


# ---------------------------------------------------------------------------
# Register / login
# ---------------------------------------------------------------------------


def test_register_returns_a_usable_session(client: TestClient) -> None:
    body = register(client)
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "owner@example.com"  # normalised
    assert "password" not in str(body)

    me = client.get("/api/auth/me", headers=auth_header(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"


def test_login_is_case_insensitive_on_email(client: TestClient) -> None:
    register(client)
    response = client.post(
        "/api/auth/login",
        json={"email": "OWNER@example.com", "password": CREDENTIALS["password"]},
    )
    assert response.status_code == 200


def test_duplicate_registration_is_refused(client: TestClient) -> None:
    register(client)
    response = client.post("/api/auth/register", json=CREDENTIALS)
    assert response.status_code == 409


def test_wrong_password_and_unknown_user_are_indistinguishable(
    client: TestClient,
) -> None:
    """No account-existence oracle: identical status and identical wording."""
    register(client)
    wrong = client.post(
        "/api/auth/login", json={"email": CREDENTIALS["email"], "password": "nope"}
    )
    missing = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_short_password_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/auth/register", json={"email": "a@b.com", "password": "short"}
    )
    assert response.status_code == 422


def test_invalid_email_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/auth/register", json={"email": "not-an-email", "password": "long enough"}
    )
    assert response.status_code == 422


def test_registration_can_be_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    get_settings.cache_clear()
    response = client.post("/api/auth/register", json=CREDENTIALS)
    assert response.status_code == 403


def test_protected_endpoints_require_a_token(client: TestClient) -> None:
    for path in ("/api/auth/me", "/api/temperature-events"):
        assert client.get(path).status_code == 401


def test_bad_authorization_header_is_unauthenticated(client: TestClient) -> None:
    register(client)
    for header in ({"Authorization": "Basic abc"}, {"Authorization": "Bearer"}):
        assert client.get("/api/auth/me", headers=header).status_code == 401


# ---------------------------------------------------------------------------
# Emergency contact
# ---------------------------------------------------------------------------


def test_emergency_contact_round_trip(client: TestClient) -> None:
    token = register(client)["access_token"]
    response = client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={
            "emergency_email": "Contact@Example.com",
            "emergency_name": "Neighbour",
            "temperature_limit_c": 48.0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["emergency_email"] == "contact@example.com"
    assert body["temperature_limit_c"] == 48.0


def test_emergency_contact_can_be_cleared(client: TestClient) -> None:
    token = register(client)["access_token"]
    client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={"emergency_email": "contact@example.com"},
    )
    response = client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={"emergency_email": ""},
    )
    assert response.json()["emergency_email"] == ""


def test_absurd_temperature_limits_are_refused(client: TestClient) -> None:
    token = register(client)["access_token"]
    for limit in (10.0, 400.0):
        response = client.put(
            "/api/auth/emergency-contact",
            headers=auth_header(token),
            json={"temperature_limit_c": limit},
        )
        assert response.status_code == 422


def test_partial_update_leaves_other_fields_alone(client: TestClient) -> None:
    token = register(client)["access_token"]
    client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={"emergency_email": "contact@example.com", "temperature_limit_c": 44.0},
    )
    body = client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={"notifications_enabled": False},
    ).json()
    assert body["emergency_email"] == "contact@example.com"
    assert body["temperature_limit_c"] == 44.0
    assert body["notifications_enabled"] is False


def test_notification_status_reports_missing_transport(client: TestClient) -> None:
    token = register(client)["access_token"]
    body = client.get(
        "/api/auth/notification-status", headers=auth_header(token)
    ).json()
    assert body["transport"] == "none"
    assert body["configured"] is False
    assert body["contact_set"] is False


# ---------------------------------------------------------------------------
# Device ownership
# ---------------------------------------------------------------------------


def test_claiming_a_device(client: TestClient, sample_telemetry: dict) -> None:
    client.post("/api/telemetry", json=sample_telemetry)
    token = register(client)["access_token"]
    response = client.post(
        "/api/devices/claim",
        headers=auth_header(token),
        json={"device_id": "esp32-test-01"},
    )
    assert response.status_code == 200


def test_claiming_an_unknown_device_is_404(client: TestClient) -> None:
    token = register(client)["access_token"]
    response = client.post(
        "/api/devices/claim", headers=auth_header(token), json={"device_id": "ghost"}
    )
    assert response.status_code == 404


def test_a_device_cannot_be_stolen(client: TestClient, sample_telemetry: dict) -> None:
    """Redirecting someone else's fire alarm to your inbox must be impossible."""
    client.post("/api/telemetry", json=sample_telemetry)
    first = register(client)["access_token"]
    client.post(
        "/api/devices/claim",
        headers=auth_header(first),
        json={"device_id": "esp32-test-01"},
    )
    second = register(client, email="other@example.com")["access_token"]
    response = client.post(
        "/api/devices/claim",
        headers=auth_header(second),
        json={"device_id": "esp32-test-01"},
    )
    assert response.status_code == 409


def test_reclaiming_your_own_device_is_fine(
    client: TestClient, sample_telemetry: dict
) -> None:
    client.post("/api/telemetry", json=sample_telemetry)
    token = register(client)["access_token"]
    for _ in range(2):
        response = client.post(
            "/api/devices/claim",
            headers=auth_header(token),
            json={"device_id": "esp32-test-01"},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Temperature escalation
# ---------------------------------------------------------------------------


class _RecordingNotifier(EmailNotifier):
    """Captures messages instead of sending them, and can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[EmergencyMessage] = []
        self.fail = fail
        self.done = threading.Event()

    @property
    def transport(self) -> str:
        return "smtp"

    def send(self, message: EmergencyMessage) -> None:
        try:
            if self.fail:
                raise RuntimeError("smtp exploded")
            self.sent.append(message)
        finally:
            self.done.set()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _RecordingNotifier:
    notifier = _RecordingNotifier()
    monkeypatch.setattr(notify, "get_notifier", lambda: notifier)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "get_notifier", lambda: notifier)
    return notifier


def _own_device(client: TestClient, sample_telemetry: dict, **contact: Any) -> str:
    client.post("/api/telemetry", json=sample_telemetry)
    token = register(client)["access_token"]
    client.post(
        "/api/devices/claim",
        headers=auth_header(token),
        json={"device_id": sample_telemetry["device_id"]},
    )
    client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={
            "emergency_email": "contact@example.com",
            "temperature_limit_c": 55.0,
            **contact,
        },
    )
    return token


def test_breach_records_an_event_and_emails(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 71.0})

    assert recorded.done.wait(timeout=5)
    assert len(recorded.sent) == 1
    message = recorded.sent[0]
    assert message.to_email == "contact@example.com"
    assert message.temperature_c == 71.0
    assert message.threshold_c == 55.0

    events = client.get(
        "/api/temperature-events", headers=auth_header(token)
    ).json()
    assert len(events) == 1
    assert events[0]["trigger_temperature_c"] == 71.0
    assert events[0]["device_id"] == "esp32-test-01"


def test_normal_temperature_records_nothing(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 24.0})
    assert recorded.sent == []
    assert client.get("/api/temperature-events", headers=auth_header(token)).json() == []


def test_one_excursion_is_one_event_and_one_email(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    """A fire burning for a minute must not send thirty emails."""
    token = _own_device(client, sample_telemetry)
    for temperature in (60.0, 68.0, 75.0, 81.0, 77.0):
        client.post(
            "/api/telemetry", json={**sample_telemetry, "temperature_c": temperature}
        )
    assert recorded.done.wait(timeout=5)
    assert len(recorded.sent) == 1

    events = client.get("/api/temperature-events", headers=auth_header(token)).json()
    assert len(events) == 1
    assert events[0]["peak_temperature_c"] == 81.0


def test_excursion_closes_when_it_cools(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 22.0})
    events = client.get("/api/temperature-events", headers=auth_header(token)).json()
    assert events[0]["ended_at"] is not None


def test_cooldown_blocks_a_second_email(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})
    assert recorded.done.wait(timeout=5)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 20.0})
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})

    assert len(recorded.sent) == 1
    events = client.get("/api/temperature-events", headers=auth_header(token)).json()
    # Both excursions are recorded even though only one was emailed: the
    # history must stay complete regardless of notification policy.
    assert len(events) == 2


def test_cooldown_expires(
    client: TestClient,
    sample_telemetry: dict,
    recorded: _RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMERGENCY_COOLDOWN_S", "0")
    get_settings.cache_clear()
    _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})
    assert recorded.done.wait(timeout=5)
    recorded.done.clear()
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 20.0})
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 72.0})
    assert recorded.done.wait(timeout=5)
    assert len(recorded.sent) == 2


def test_muting_stops_email_and_recording(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    token = _own_device(client, sample_telemetry, notifications_enabled=False)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 90.0})
    assert recorded.sent == []
    assert client.get("/api/temperature-events", headers=auth_header(token)).json() == []


def test_unclaimed_device_emails_nobody(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    """Nobody has claimed this node, so there is no defensible recipient."""
    register(client)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 95.0})
    assert recorded.sent == []


def test_fallback_recipient_covers_unclaimed_devices(
    client: TestClient,
    sample_telemetry: dict,
    recorded: _RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FALLBACK_EMERGENCY_EMAIL", "ops@example.com")
    get_settings.cache_clear()
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 95.0})
    assert recorded.done.wait(timeout=5)
    assert recorded.sent[0].to_email == "ops@example.com"


def test_events_are_private_to_their_account(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})
    assert recorded.done.wait(timeout=5)

    other = register(client, email="stranger@example.com")["access_token"]
    assert client.get(
        "/api/temperature-events", headers=auth_header(other)
    ).json() == []


def test_missing_transport_is_recorded_on_the_event(
    client: TestClient, sample_telemetry: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-failure case: a contact is set but the server cannot send.

    Regression for a real ordering bug. The outcome used to be written through
    a *second* database session while the ingest transaction was still open, so
    the freshly-flushed event row was invisible and the write hit nothing. The
    history then showed "no email sent" with no reason, which is precisely the
    situation where the user most needs to be told what went wrong.
    """
    notifier = EmailNotifier()  # genuinely unconfigured
    monkeypatch.setattr(notify, "get_notifier", lambda: notifier)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "get_notifier", lambda: notifier)

    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})

    events = client.get("/api/temperature-events", headers=auth_header(token)).json()
    assert len(events) == 1
    assert events[0]["notified"] is False
    assert "no email transport configured" in events[0]["notify_error"]


def test_result_is_recorded_even_when_the_send_beats_the_commit(
    client: TestClient, sample_telemetry: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport that returns instantly must not lose its audit record."""
    notifier = _RecordingNotifier()
    monkeypatch.setattr(notify, "get_notifier", lambda: notifier)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "get_notifier", lambda: notifier)

    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})
    assert notifier.done.wait(timeout=5)

    events: list[dict] = []
    for _ in range(60):
        events = client.get(
            "/api/temperature-events", headers=auth_header(token)
        ).json()
        if events and events[0]["notified"]:
            break
        threading.Event().wait(0.05)
    assert events[0]["notified"] is True
    assert events[0]["notify_error"] == ""


def test_a_failed_send_is_recorded_not_swallowed(
    client: TestClient, sample_telemetry: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = _RecordingNotifier(fail=True)
    monkeypatch.setattr(notify, "get_notifier", lambda: notifier)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "get_notifier", lambda: notifier)

    token = _own_device(client, sample_telemetry)
    client.post("/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0})
    assert notifier.done.wait(timeout=5)

    for _ in range(50):
        events = client.get(
            "/api/temperature-events", headers=auth_header(token)
        ).json()
        if events and events[0]["notify_error"]:
            break
        threading.Event().wait(0.05)
    assert events[0]["notified"] is False
    assert "smtp exploded" in events[0]["notify_error"]


def test_email_failure_does_not_break_ingest(
    client: TestClient, sample_telemetry: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fire reading must reach the database even if Gmail is down."""
    notifier = _RecordingNotifier(fail=True)
    monkeypatch.setattr(notify, "get_notifier", lambda: notifier)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "get_notifier", lambda: notifier)

    _own_device(client, sample_telemetry)
    response = client.post(
        "/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0}
    )
    assert response.status_code == 200
    assert client.get("/api/readings").json()


def test_camera_node_without_weather_never_escalates(
    client: TestClient, recorded: _RecordingNotifier
) -> None:
    """A camera has no thermometer, so it cannot breach a temperature limit."""
    token = register(client)["access_token"]
    client.post(
        "/api/camera/telemetry",
        json={
            "device_id": "cam-1",
            "flame_ratio": 0.4,
            "luminance": 0.6,
            "haze_index": 0.3,
        },
    )
    client.post(
        "/api/devices/claim", headers=auth_header(token), json={"device_id": "cam-1"}
    )
    client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={"emergency_email": "contact@example.com"},
    )
    client.post(
        "/api/camera/telemetry",
        json={
            "device_id": "cam-1",
            "flame_ratio": 0.4,
            "luminance": 0.6,
            "haze_index": 0.3,
        },
    )
    assert recorded.sent == []


def test_camera_node_with_shared_weather_can_escalate(
    client: TestClient, recorded: _RecordingNotifier
) -> None:
    token = register(client)["access_token"]
    frame = {
        "device_id": "cam-1",
        "flame_ratio": 0.02,
        "luminance": 0.5,
        "haze_index": 0.2,
        "temperature_c": 20.0,
    }
    client.post("/api/camera/telemetry", json=frame)
    client.post(
        "/api/devices/claim", headers=auth_header(token), json={"device_id": "cam-1"}
    )
    client.put(
        "/api/auth/emergency-contact",
        headers=auth_header(token),
        json={"emergency_email": "contact@example.com", "temperature_limit_c": 45.0},
    )
    client.post("/api/camera/telemetry", json={**frame, "temperature_c": 61.0})

    assert recorded.done.wait(timeout=5)
    assert recorded.sent[0].sensor_kind == "camera"


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def _message(**overrides: Any) -> EmergencyMessage:
    base: dict[str, Any] = {
        "to_email": "contact@example.com",
        "to_name": "Neighbour",
        "device_id": "esp32-01",
        "location": "Kitchen",
        "temperature_c": 72.4,
        "threshold_c": 55.0,
        "sensor_kind": "hardware",
        "occurred_at": datetime(2026, 7, 26, 17, 30, tzinfo=timezone.utc),
        "dashboard_url": "https://fireprotect.onrender.com",
    }
    base.update(overrides)
    return EmergencyMessage(**base)


def test_subject_is_readable_on_a_lock_screen() -> None:
    """It may be all the recipient reads before acting."""
    subject = _message().subject
    assert "72.4" in subject
    assert "Kitchen" in subject
    assert subject.startswith("FIRE ALERT")


def test_subject_falls_back_to_device_id_without_a_location() -> None:
    assert "esp32-01" in _message(location="").subject


def test_body_carries_the_safety_disclaimer() -> None:
    for body in (_message().body_text(), _message().body_html()):
        # Whitespace-normalised: the plain-text body hard-wraps at 72 columns,
        # so the phrase legitimately spans a newline.
        flat = " ".join(body.replace("&nbsp;", " ").split())
        assert "emergency number" in flat
        assert "certified life-safety device" in flat


def test_body_names_the_sensor_family() -> None:
    assert "camera" in _message(sensor_kind="camera").body_text().lower()
    assert "gas" in _message(sensor_kind="hardware").body_text().lower()


def test_dashboard_link_is_omitted_when_unset() -> None:
    assert "href" not in _message(dashboard_url="").body_html()


def test_transport_selection_prefers_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    get_settings.cache_clear()
    assert EmailNotifier().transport == "resend"


def test_partial_smtp_config_is_not_treated_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host but no credentials would fail at send time; report it up front."""
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    get_settings.cache_clear()
    notifier = EmailNotifier()
    assert notifier.transport == "none"
    assert not notifier.configured


def test_sending_without_a_transport_raises_a_useful_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        EmailNotifier().send(_message())


def test_cooldown_uses_naive_timestamps_safely(
    client: TestClient, sample_telemetry: dict, recorded: _RecordingNotifier
) -> None:
    """SQLite hands back naive datetimes; comparing them must not explode."""
    from app.db import User, session_scope

    _own_device(client, sample_telemetry)
    with session_scope() as session:
        user = session.query(User).one()
        user.last_notified_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=5)

    response = client.post(
        "/api/telemetry", json={**sample_telemetry, "temperature_c": 70.0}
    )
    assert response.status_code == 200
    assert recorded.sent == []
