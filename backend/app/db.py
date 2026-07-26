"""Database engine, session management and ORM models."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    event,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

from app.config import get_settings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use naive datetimes in this codebase."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


#: What kind of sensor a device is. This is not cosmetic: a camera node
#: measures completely different physical quantities from an ESP32 gas node,
#: is classified by a different model, and must never have its numbers read as
#: though they came from an MQ-2. The kind travels with every reading so the
#: distinction survives all the way to the dashboard.
SENSOR_KINDS = ("hardware", "camera")


class Device(Base):
    """A sensor node: an ESP32 (or simulator), or a browser camera."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(16), default="hardware", index=True)
    #: Which account this node belongs to. NULL for anything that reported
    #: before anyone claimed it — an unregistered ESP32, the demo device. An
    #: unowned node alarms on the dashboard but mails nobody, because there is
    #: nobody it could correctly be said to belong to.
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    firmware_version: Mapped[str] = mapped_column(String(32), default="")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    online: Mapped[bool] = mapped_column(Boolean, default=True)

    readings: Mapped[list[Reading]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    owner: Mapped[User | None] = relationship(back_populates="devices")


class Reading(Base):
    """One telemetry sample plus both models' classifications."""

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    #: Which sensor family produced this row. Copied from the device rather
    #: than joined at read time so a reading is self-describing: exports, the
    #: WebSocket feed and the CSV dump all carry it without a join.
    sensor_kind: Mapped[str] = mapped_column(String(16), default="hardware")

    # --- gas / thermal channels (ESP32 hardware) ---------------------------
    # Nullable because a camera node genuinely does not measure these. Writing
    # 0.0 instead would render as "0 ppm — clean air" on the dashboard, which
    # is a confident false statement about a sensor that does not exist. NULL
    # renders as "not measured", which is true.
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    smoke_ppm: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_quality_ppm: Mapped[float | None] = mapped_column(Float, nullable=True)
    flame_analog_volts: Mapped[float | None] = mapped_column(Float, nullable=True)
    flame_detected: Mapped[int] = mapped_column(Integer, default=0)

    # --- camera channels ---------------------------------------------------
    # NULL on hardware rows and NULL on camera rows for the gas columns above.
    # Nullable rather than zero-filled is the whole point: 0.0 ppm reads as
    # "clean air", which would be a lie about a sensor that cannot smell.
    flame_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    luminance: Mapped[float | None] = mapped_column(Float, nullable=True)
    haze_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    flicker: Mapped[float | None] = mapped_column(Float, nullable=True)

    temp_rate_c_per_min: Mapped[float] = mapped_column(Float, default=0.0)
    smoke_rate_ppm_per_min: Mapped[float] = mapped_column(Float, default=0.0)
    heat_index_c: Mapped[float] = mapped_column(Float, default=0.0)

    #: What the on-device decision tree decided, as reported by the device.
    device_status: Mapped[str] = mapped_column(String(16), default="SAFE")
    #: What the server-side random forest decided.
    server_status: Mapped[str] = mapped_column(String(16), default="SAFE", index=True)
    #: P(FIRE) from the random forest, used for the risk gauge.
    fire_probability: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)

    device: Mapped[Device] = relationship(back_populates="readings")

    __table_args__ = (
        Index("ix_readings_device_recorded", "device_id", "recorded_at"),
    )


class Alert(Base):
    """A raised alert. Alerts are append-only; resolution sets ``resolved_at``."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    severity: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(String(512))
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    temperature_c: Mapped[float] = mapped_column(Float, default=0.0)
    smoke_ppm: Mapped[float] = mapped_column(Float, default=0.0)
    fire_probability: Mapped[float] = mapped_column(Float, default=0.0)

    device: Mapped[Device] = relationship(back_populates="alerts")


class User(Base):
    """An account. Owns devices, an emergency contact and a history.

    Passwords are never stored. ``password_hash`` is a bcrypt digest, which is
    deliberately slow to compute: if this table ever leaks, an attacker gets
    weeks of GPU time per password instead of a rainbow-table lookup.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Stored lower-cased; login is case-insensitive because nobody remembers
    #: whether they signed up with a capital letter.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # --- emergency contact ------------------------------------------------
    #: Where the emergency mail goes. Empty means notifications are off, which
    #: is the safe default: silently mailing a stranger would be worse than
    #: sending nothing.
    emergency_email: Mapped[str] = mapped_column(String(320), default="")
    emergency_name: Mapped[str] = mapped_column(String(128), default="")
    #: Temperature that counts as an emergency for this account, in Celsius.
    #: 55 C is above any normal indoor condition (a hot attic peaks near 50)
    #: but well below flashover, so it fires early enough to matter.
    temperature_limit_c: Mapped[float] = mapped_column(Float, default=55.0)
    #: Master switch, so a contact can be kept on file while muted.
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Guards against an alert storm mailing the same person every 2 seconds.
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    devices: Mapped[list[Device]] = relationship(back_populates="owner")
    events: Mapped[list[TemperatureEvent]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class TemperatureEvent(Base):
    """A recorded high-temperature excursion.

    Separate from ``readings`` on purpose. Readings are high-volume and pruned
    on a retention schedule; these are the handful of moments that actually
    mattered, and they are kept indefinitely so the history a user opens next
    year is still there.
    """

    __tablename__ = "temperature_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: NULL when the device that produced it is unclaimed. The excursion is
    #: still worth recording — it is evidence something got hot — it just has
    #: no account to appear under.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Temperature that opened the excursion, and the worst seen while it ran.
    trigger_temperature_c: Mapped[float] = mapped_column(Float)
    peak_temperature_c: Mapped[float] = mapped_column(Float)
    threshold_c: Mapped[float] = mapped_column(Float)
    sensor_kind: Mapped[str] = mapped_column(String(16), default="hardware")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_error: Mapped[str] = mapped_column(String(512), default="")

    user: Mapped[User] = relationship(back_populates="events")


class Threshold(Base):
    """User-configurable alert thresholds. A single row per key."""

    __tablename__ = "thresholds"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


#: Defaults seeded on first start. Keys are referenced by the frontend's
#: threshold-config panel and by ``alerts.evaluate``.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "temperature_warning_c": 45.0,
    "temperature_fire_c": 58.0,
    "smoke_warning_ppm": 900.0,
    "smoke_fire_ppm": 2200.0,
    "air_quality_warning_ppm": 300.0,
    "flame_volts": 1.15,
    "fire_probability_alert": 0.6,
}


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {}
    if url.startswith("sqlite"):
        # check_same_thread=False: the MQTT client runs on its own thread and
        # must be able to write. Sessions are still never shared across threads.
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 15.0
        if ":memory:" in url:
            # An in-memory DB is per-connection unless we pin a single one.
            kwargs["poolclass"] = StaticPool
    return create_engine(url, connect_args=connect_args, future=True, **kwargs)  # type: ignore[arg-type]


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    """Enable WAL and enforce foreign keys on SQLite connections.

    WAL matters here: the MQTT ingest thread writes continuously while HTTP
    requests read. Without WAL, SQLite's default rollback journal serialises
    readers against the writer and produces "database is locked" under load.
    """
    if not hasattr(dbapi_connection, "cursor"):  # pragma: no cover
        return
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    except Exception:  # pragma: no cover - non-SQLite backends
        pass
    finally:
        cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionFactory


def configure(url: str) -> None:
    """Point the module at a different database. Used by tests."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


#: Columns added after the first release. ``create_all`` only creates missing
#: *tables* — it will not touch a table that already exists, so upgrading an
#: existing database needs an explicit ALTER. This project is small enough that
#: a full migration tool (Alembic) would be more machinery than the problem
#: deserves; a declared list of additive, nullable columns covers every schema
#: change made so far and fails loudly if one ever isn't additive.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "devices": {
        "kind": "VARCHAR(16) DEFAULT 'hardware'",
        "owner_id": "INTEGER",
    },
    "readings": {
        "sensor_kind": "VARCHAR(16) DEFAULT 'hardware'",
        "flame_ratio": "FLOAT",
        "luminance": "FLOAT",
        "haze_index": "FLOAT",
        "flicker": "FLOAT",
    },
}


def _migrate_added_columns() -> None:
    """Add any missing columns to existing tables. Idempotent and additive.

    Deliberately does not drop, rename or retype anything: those need real
    migrations with real data handling, and silently doing them here would be
    a good way to lose someone's history.
    """
    engine = get_engine()
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all just made it, with every column present
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in present:
                    continue
                logger.info("migrating: adding %s.%s", table, name)
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    """Create tables, apply additive migrations, seed thresholds. Idempotent."""
    Base.metadata.create_all(get_engine())
    _migrate_added_columns()
    with session_scope() as session:
        existing = {row.key for row in session.query(Threshold).all()}
        for key, value in DEFAULT_THRESHOLDS.items():
            if key not in existing:
                session.add(Threshold(key=key, value=value))


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope. Commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
