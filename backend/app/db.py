"""Database engine, session management and ORM models."""

from __future__ import annotations

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
)
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


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use naive datetimes in this codebase."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Device(Base):
    """A physical or simulated ESP32 node."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
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

    temperature_c: Mapped[float] = mapped_column(Float)
    humidity_pct: Mapped[float] = mapped_column(Float)
    smoke_ppm: Mapped[float] = mapped_column(Float)
    air_quality_ppm: Mapped[float] = mapped_column(Float)
    flame_analog_volts: Mapped[float] = mapped_column(Float)
    flame_detected: Mapped[int] = mapped_column(Integer)

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


def init_db() -> None:
    """Create tables and seed default thresholds. Idempotent."""
    Base.metadata.create_all(get_engine())
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
