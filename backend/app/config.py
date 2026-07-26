"""Application settings, loaded from the environment with sane defaults."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be overridden by an environment variable of the same name
    (case-insensitive). See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- MQTT ------------------------------------------------------------
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_keepalive: int = 60
    mqtt_client_id: str = "fireprotect-backend"
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_topic_prefix: str = "fireprotect"
    #: Seconds to wait between reconnect attempts, doubling up to the max.
    mqtt_reconnect_min_s: float = 1.0
    mqtt_reconnect_max_s: float = 30.0

    # --- Database --------------------------------------------------------
    #: SQLite by default so a fresh clone runs with nothing installed. Point it
    #: at Supabase (or any Postgres) for persistence that survives a redeploy:
    #: Render's filesystem is ephemeral, so on SQLite every restart wipes the
    #: history, the accounts and the emergency contacts.
    database_url: str = f"sqlite:///{REPO_ROOT / 'backend' / 'fireprotect.db'}"
    #: Readings older than this are pruned by the retention job.
    retention_days: int = 30

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Make the URL Supabase hands you actually work with SQLAlchemy 2.

        Supabase's dashboard gives a `postgresql://...` (or, on older pages,
        `postgres://...`) URI. SQLAlchemy resolves a bare `postgresql://` to
        psycopg2, which is not installed here — this project uses psycopg 3 —
        and rejects `postgres://` outright. Both produce an import error at
        boot that reads like a missing dependency rather than a URL scheme
        problem, so it is fixed here instead of in a troubleshooting doc.

        Also forces TLS: Supabase accepts unencrypted connections, and a
        database password crossing the public internet in the clear is not
        something to leave to a default.
        """
        url = value.strip()
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+psycopg://" + url[len(prefix) :]
                break
        if url.startswith("postgresql+psycopg://") and "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return url

    # --- Models ----------------------------------------------------------
    model_dir: Path = REPO_ROOT / "ml"

    # --- Dashboard -------------------------------------------------------
    #: Built frontend. When present the API serves it at "/", so the whole
    #: system is reachable on one port with no second dev server.
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    # --- ThingSpeak ------------------------------------------------------
    thingspeak_enabled: bool = False
    thingspeak_write_api_key: str | None = None
    thingspeak_url: str = "https://api.thingspeak.com/update"
    thingspeak_timeout_s: float = 10.0
    #: ThingSpeak's free tier rejects updates faster than one per 15 s.
    thingspeak_min_interval_s: float = 15.0
    thingspeak_max_retries: int = 4
    thingspeak_buffer_path: Path = REPO_ROOT / "backend" / "thingspeak_buffer.jsonl"
    thingspeak_buffer_max_entries: int = 5000

    # --- Alerting --------------------------------------------------------
    #: A device is considered offline after this long without telemetry.
    device_offline_after_s: float = 30.0
    #: Consecutive FIRE classifications required before raising an alert.
    #: 1 would alarm on a single noisy sample; 2 is the smallest value that
    #: requires corroboration without meaningfully delaying a real fire
    #: (2 samples = 4 s at the 2 s publish rate).
    fire_alert_consecutive: int = 2
    warning_alert_consecutive: int = 3

    # --- API -------------------------------------------------------------
    #: Loopback by default. The API has no authentication, and it can change
    #: fire-alert thresholds, delete readings and accept injected telemetry —
    #: binding 0.0.0.0 would expose all of that to anyone on the same network.
    #: Set API_HOST=0.0.0.0 deliberately (and put auth in front of it) to serve
    #: real ESP32 devices on a LAN. The Docker compose file sets it explicitly,
    #: because inside a container binding loopback would make the published
    #: port unreachable.
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    #: Origins allowed to open the WebSocket. Browsers do NOT apply CORS to
    #: WebSocket handshakes, so without an explicit Origin check any website
    #: the user visits could connect to a locally running FireProtect and read
    #: their live sensor stream (cross-site WebSocket hijacking). Non-browser
    #: clients send no Origin header and are allowed through.
    websocket_strict_origin: bool = True
    #: Max readings returned by a single history query.
    max_history_limit: int = 5000

    #: `NoDecode` is load-bearing. pydantic-settings JSON-decodes complex
    #: types straight out of the environment, *before* any field validator
    #: runs — so `CORS_ORIGINS=https://x.vercel.app` raised
    #: `SettingsError: error parsing value` and the app died at boot. Every
    #: hosting dashboard gives you a plain text box, so that is the form people
    #: actually type. NoDecode hands the raw string to `_split_origins` below.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:4173",
            "http://localhost:3000",
        ]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string as well as a JSON list.

        Hosting dashboards (Render, Vercel, Railway) give you a plain text box
        for environment variables. Requiring valid JSON there means
        `CORS_ORIGINS=https://x.vercel.app` fails to parse with an opaque
        error, which is a miserable way to lose an afternoon.
        """
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                # Parse the JSON form here rather than passing the string on:
                # a `before` validator short-circuits pydantic's own JSON
                # handling, so returning the raw string fails as `list_type`.
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"CORS_ORIGINS looks like JSON but is not valid: {exc}"
                    ) from exc
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS JSON must be a list of strings")
                return parsed
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    # --- Accounts --------------------------------------------------------
    #: HS256 signing key for session tokens. MUST be set in any deployment you
    #: care about: when it is empty a random key is generated at boot, which
    #: logs everyone out on every restart and breaks completely across more
    #: than one instance. Empty is nonetheless the default, because a shipped
    #: default secret is a forgeable-token vulnerability in every install.
    jwt_secret: str = ""
    jwt_ttl_hours: float = 24.0 * 14

    #: Allow new accounts. Turn off once your users have signed up if the
    #: deployment is public and you do not want strangers registering.
    allow_registration: bool = True

    # --- Emergency email -------------------------------------------------
    #: Resend takes priority when both are set (see app/notify.py).
    resend_api_key: str = ""

    #: Gmail: smtp.gmail.com:587, username = your address, password = a 16
    #: character Google App Password (NOT your account password - that will be
    #: rejected, and you should never paste it into a server anyway).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_starttls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_s: float = 20.0

    mail_from: str = ""
    mail_from_name: str = "FireProtect"

    #: Included as a link in the alert email. Set it to your deployed URL.
    public_url: str = ""

    #: Minimum gap between emergency emails for one account. Alerts are
    #: re-evaluated every couple of seconds; without this, one fire would send
    #: hundreds of messages and get the sender blocked as spam.
    emergency_cooldown_s: float = 600.0

    #: Fallback recipient for devices no account has claimed - an ESP32 that
    #: reported before anyone registered, for instance. Empty means unowned
    #: devices alarm on the dashboard but mail nobody, which is correct: there
    #: is no defensible person to notify.
    fallback_emergency_email: str = ""
    fallback_temperature_limit_c: float = 55.0

    # --- Demo mode -------------------------------------------------------
    #: Generate telemetry in-process. A deployed instance has no ESP32 and
    #: nobody running the simulator, so without this the dashboard is empty
    #: for every visitor who opens the link. Off by default: a real
    #: deployment with real sensors must never have synthetic readings mixed
    #: into its history.
    demo_mode: bool = False
    demo_device_id: str = "esp32-demo-01"
    demo_location: str = "Demo Living Room"
    demo_interval_s: float = 2.0
    #: Simulated seconds advanced per tick. Higher than the interval replays
    #: events faster than real time, so a visitor sees a fire develop in about
    #: a minute rather than three.
    demo_sim_step_s: float = 4.0

    @property
    def telemetry_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/telemetry"

    @property
    def alert_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/alert"

    @property
    def status_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/+/status"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
