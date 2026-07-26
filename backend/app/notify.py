"""Emergency email delivery.

Two transports, chosen by whichever credentials are present:

* **Gmail SMTP** — send from the user's own address with a Google App Password.
  Free, no signup, and the mail arrives from an address the recipient already
  trusts, which matters when the message is "your house may be on fire".
  Requires 2-Step Verification on the Google account.
* **Resend** — a transactional email API. More reliable at volume and does not
  put a Google account credential on the server, but mail comes from a Resend
  sender unless a domain is verified.

Resend wins if both are configured, because an API call fails fast and loudly
while SMTP can hang for the full socket timeout.

Design constraints this file exists to satisfy
----------------------------------------------
1. **Sending must never block ingest.** A fire alert has to reach the database
   and the dashboard whether or not Gmail is reachable. Every send runs on a
   worker thread and every failure is caught, logged and recorded against the
   event rather than raised.
2. **One fire is one email.** Alerts re-evaluate every two seconds. Without the
   cooldown in ``ingest``, a single event would mail the contact hundreds of
   times and get the sender marked as spam.
3. **The message has to be useful on a lock screen.** The subject alone carries
   the temperature, the device and the location, because that may be all the
   recipient reads before acting.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class EmergencyMessage:
    """Everything the email needs, resolved before any I/O is attempted."""

    to_email: str
    to_name: str
    device_id: str
    location: str
    temperature_c: float
    threshold_c: float
    sensor_kind: str
    occurred_at: datetime
    dashboard_url: str = ""

    @property
    def subject(self) -> str:
        where = self.location or self.device_id
        return (
            f"FIRE ALERT: {self.temperature_c:.1f}°C at {where} "
            f"(limit {self.threshold_c:.0f}°C)"
        )

    def body_text(self) -> str:
        source = (
            "camera node (visual flame detection)"
            if self.sensor_kind == "camera"
            else "sensor node (gas + thermal)"
        )
        lines = [
            "FIREPROTECT EMERGENCY ALERT",
            "",
            f"A temperature of {self.temperature_c:.1f} °C was recorded, above "
            f"the configured emergency limit of {self.threshold_c:.0f} °C.",
            "",
            f"  Device    {self.device_id}",
            f"  Location  {self.location or 'not set'}",
            f"  Source    {source}",
            f"  Time      {self.occurred_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        if self.dashboard_url:
            lines += ["", f"Live dashboard: {self.dashboard_url}"]
        lines += [
            "",
            "-" * 60,
            "If this is a real fire, call your local emergency number now.",
            "",
            "FireProtect is a monitoring project, not a certified life-safety",
            "device. It carries no UL 217 or EN 54 listing and must not be relied",
            "on in place of a listed smoke alarm.",
        ]
        return "\n".join(lines)

    def body_html(self) -> str:
        source = (
            "Camera node (visual flame detection)"
            if self.sensor_kind == "camera"
            else "Sensor node (gas + thermal)"
        )
        link = (
            f'<p style="margin:24px 0"><a href="{self.dashboard_url}" '
            'style="background:#fff;color:#000;padding:12px 20px;'
            'text-decoration:none;font-weight:600;display:inline-block">'
            "Open the live dashboard</a></p>"
            if self.dashboard_url
            else ""
        )
        return f"""\
<!doctype html>
<html><body style="margin:0;background:#0a0a0a;color:#f5f5f5;
 font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<div style="max-width:560px;margin:0 auto;padding:32px 24px">
  <p style="letter-spacing:.18em;font-size:11px;text-transform:uppercase;
     color:#ff5a1f;margin:0 0 8px">FireProtect emergency alert</p>
  <h1 style="font-size:34px;line-height:1.1;margin:0 0 20px">
    {self.temperature_c:.1f}&deg;C recorded</h1>
  <p style="color:#a3a3a3;font-size:16px;line-height:1.6;margin:0 0 24px">
    This is above the emergency limit of {self.threshold_c:.0f}&deg;C set for
    this account.</p>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:8px 0;color:#a3a3a3">Device</td>
        <td style="padding:8px 0;text-align:right">{self.device_id}</td></tr>
    <tr><td style="padding:8px 0;color:#a3a3a3">Location</td>
        <td style="padding:8px 0;text-align:right">
          {self.location or "not set"}</td></tr>
    <tr><td style="padding:8px 0;color:#a3a3a3">Source</td>
        <td style="padding:8px 0;text-align:right">{source}</td></tr>
    <tr><td style="padding:8px 0;color:#a3a3a3">Time</td>
        <td style="padding:8px 0;text-align:right">
          {self.occurred_at.strftime("%Y-%m-%d %H:%M:%S UTC")}</td></tr>
  </table>
  {link}
  <p style="border-top:1px solid #333;padding-top:20px;color:#ff5a1f;
     font-size:14px;font-weight:600">
    If this is a real fire, call your local emergency number now.</p>
  <p style="color:#737373;font-size:12px;line-height:1.6">
    FireProtect is a monitoring project, not a certified life-safety device.
    It carries no UL&nbsp;217 or EN&nbsp;54 listing and must not be relied on
    in place of a listed smoke alarm.</p>
</div></body></html>"""


class EmailNotifier:
    """Sends emergency mail through whichever transport is configured."""

    @property
    def transport(self) -> str:
        """``"resend"``, ``"smtp"`` or ``"none"``."""
        settings = get_settings()
        if settings.resend_api_key:
            return "resend"
        if settings.smtp_host and settings.smtp_username and settings.smtp_password:
            return "smtp"
        return "none"

    @property
    def configured(self) -> bool:
        return self.transport != "none"

    def send(self, message: EmergencyMessage) -> None:
        """Deliver one message. Raises on failure so the caller can record it.

        Callers run this off the request path; see ``ingest._notify_emergency``.
        """
        transport = self.transport
        if transport == "resend":
            self._send_resend(message)
        elif transport == "smtp":
            self._send_smtp(message)
        else:
            raise RuntimeError(
                "No email transport configured. Set RESEND_API_KEY, or "
                "SMTP_HOST + SMTP_USERNAME + SMTP_PASSWORD."
            )

    # -- transports --------------------------------------------------------

    def _from_pair(self) -> tuple[str, str]:
        settings = get_settings()
        address = settings.mail_from or settings.smtp_username or "alerts@fireprotect"
        return settings.mail_from_name or "FireProtect", address

    def _send_smtp(self, message: EmergencyMessage) -> None:
        settings = get_settings()
        from_name, from_address = self._from_pair()

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = formataddr((from_name, from_address))
        email["To"] = (
            formataddr((message.to_name, message.to_email))
            if message.to_name
            else message.to_email
        )
        # Emergency mail must not sit behind a queue of newsletters.
        email["X-Priority"] = "1"
        email["Importance"] = "high"
        email.set_content(message.body_text())
        email.add_alternative(message.body_html(), subtype="html")

        context = ssl.create_default_context()
        timeout = settings.smtp_timeout_s
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=timeout, context=context
            ) as server:
                server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(email)
        else:
            with smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=timeout
            ) as server:
                server.ehlo()
                if settings.smtp_use_starttls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(email)
        logger.info("emergency email sent via SMTP to %s", message.to_email)

    def _send_resend(self, message: EmergencyMessage) -> None:
        settings = get_settings()
        from_name, from_address = self._from_pair()
        response = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": f"{from_name} <{from_address}>",
                "to": [message.to_email],
                "subject": message.subject,
                "text": message.body_text(),
                "html": message.body_html(),
            },
            timeout=settings.smtp_timeout_s,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Resend rejected the message ({response.status_code}): "
                f"{response.text[:300]}"
            )
        logger.info("emergency email sent via Resend to %s", message.to_email)


_notifier: EmailNotifier | None = None


def get_notifier() -> EmailNotifier:
    global _notifier
    if _notifier is None:
        _notifier = EmailNotifier()
    return _notifier


def reset_notifier() -> None:
    global _notifier
    _notifier = None
