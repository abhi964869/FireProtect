"""FastAPI application: REST endpoints, WebSocket, lifespan wiring."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.config import get_settings
from app.db import Alert, Device, Reading, Threshold, get_db, init_db, utcnow
from app.demo import DemoDriver
from app.inference import compute_risk_score, get_classifier
from app.ingest import get_ingest_service
from app.mqtt_client import get_subscriber
from app.schemas import (
    AlertOut,
    DeviceDetail,
    DeviceOut,
    HealthOut,
    PredictRequest,
    PredictResponse,
    ReadingOut,
    StatsOut,
    TelemetryIn,
    ThresholdBulkUpdate,
    ThresholdOut,
)
from app.thingspeak import get_mirror
from app.ws import hub

logger = logging.getLogger(__name__)

VERSION = "1.0.0"

#: How often the background task sweeps for devices that stopped reporting.
OFFLINE_SWEEP_INTERVAL_S = 10.0


async def _offline_sweeper() -> None:
    """Periodically flip silent devices to offline and notify the dashboard."""
    service = get_ingest_service()
    while True:
        try:
            await asyncio.sleep(OFFLINE_SWEEP_INTERVAL_S)
            await asyncio.to_thread(service.mark_stale_devices_offline)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("offline sweep failed; continuing")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    init_db()
    get_classifier()

    hub.bind_loop(asyncio.get_running_loop())
    get_ingest_service().set_broadcaster(hub.broadcast_threadsafe)

    get_subscriber().start()
    sweeper = asyncio.create_task(_offline_sweeper())

    demo = DemoDriver() if get_settings().demo_mode else None
    if demo is not None:
        await demo.start()

    try:
        yield
    finally:
        if demo is not None:
            await demo.stop()
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        get_subscriber().stop()


app = FastAPI(
    title="FireProtect API",
    version=VERSION,
    description=(
        "IoT fire-detection backend. Subscribes to device telemetry over MQTT, "
        "classifies it with a random forest, persists it, and broadcasts live "
        "updates over WebSocket."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Health and stats
# ---------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthOut, tags=["system"])
def health(session: DbSession) -> HealthOut:
    database_ok = True
    try:
        session.execute(select(func.count()).select_from(Device))
    except Exception:
        logger.exception("database health check failed")
        database_ok = False

    mqtt_connected = get_subscriber().connected
    model_loaded = get_classifier().loaded
    pending = get_mirror().pending

    return HealthOut(
        status="ok" if database_ok else "degraded",
        database=database_ok,
        mqtt_connected=mqtt_connected,
        model_loaded=model_loaded,
        thingspeak_pending=pending,
        version=VERSION,
    )


@app.get("/api/stats", response_model=StatsOut, tags=["system"])
def stats(session: DbSession) -> StatsOut:
    total_devices = session.scalar(select(func.count()).select_from(Device)) or 0
    online_devices = (
        session.scalar(
            select(func.count()).select_from(Device).where(Device.online.is_(True))
        )
        or 0
    )
    total_readings = session.scalar(select(func.count()).select_from(Reading)) or 0
    open_alerts = (
        session.scalar(
            select(func.count()).select_from(Alert).where(Alert.resolved_at.is_(None))
        )
        or 0
    )
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    alerts_24h = (
        session.scalar(
            select(func.count()).select_from(Alert).where(Alert.triggered_at >= since)
        )
        or 0
    )

    # Current status is the worst status across all online devices' latest
    # readings - one burning room makes the whole site's status FIRE.
    latest_statuses = session.execute(
        select(Reading.server_status, Reading.fire_probability)
        .order_by(Reading.recorded_at.desc())
        .limit(50)
    ).all()
    rank = {"SAFE": 0, "WARNING": 1, "FIRE": 2}
    current = "SAFE"
    max_probability = 0.0
    for status_value, probability in latest_statuses:
        if rank.get(status_value, 0) > rank[current]:
            current = status_value
        max_probability = max(max_probability, float(probability or 0.0))

    return StatsOut(
        total_devices=total_devices,
        online_devices=online_devices,
        total_readings=total_readings,
        open_alerts=open_alerts,
        alerts_24h=alerts_24h,
        current_status=current,  # type: ignore[arg-type]
        max_fire_probability=round(max_probability, 4),
    )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@app.get("/api/devices", response_model=list[DeviceOut], tags=["devices"])
def list_devices(session: DbSession) -> list[Device]:
    return list(session.scalars(select(Device).order_by(Device.id)).all())


@app.get("/api/devices/{device_id}", response_model=DeviceDetail, tags=["devices"])
def get_device(device_id: str, session: DbSession) -> DeviceDetail:
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"device {device_id!r} not found")

    latest = session.scalars(
        select(Reading)
        .where(Reading.device_id == device_id)
        .order_by(Reading.recorded_at.desc())
        .limit(1)
    ).first()
    open_alerts = (
        session.scalar(
            select(func.count())
            .select_from(Alert)
            .where(Alert.device_id == device_id, Alert.resolved_at.is_(None))
        )
        or 0
    )

    return DeviceDetail(
        **DeviceOut.model_validate(device).model_dump(),
        latest_reading=ReadingOut.model_validate(latest) if latest else None,
        open_alerts=open_alerts,
    )


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


@app.get("/api/readings", response_model=list[ReadingOut], tags=["readings"])
def list_readings(
    session: DbSession,
    device_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    since_minutes: Annotated[int | None, Query(ge=1, le=10080)] = None,
) -> list[Reading]:
    """Most recent readings first, optionally filtered by device and window."""
    query = select(Reading).order_by(Reading.recorded_at.desc()).limit(limit)
    if device_id:
        query = query.where(Reading.device_id == device_id)
    if since_minutes:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        query = query.where(Reading.recorded_at >= cutoff)
    return list(session.scalars(query).all())


@app.get(
    "/api/devices/{device_id}/readings",
    response_model=list[ReadingOut],
    tags=["readings"],
)
def device_readings(
    device_id: str,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> list[Reading]:
    if session.get(Device, device_id) is None:
        raise HTTPException(status_code=404, detail=f"device {device_id!r} not found")
    return list(
        session.scalars(
            select(Reading)
            .where(Reading.device_id == device_id)
            .order_by(Reading.recorded_at.desc())
            .limit(limit)
        ).all()
    )


@app.post("/api/telemetry", response_model=ReadingOut, tags=["readings"])
def ingest_telemetry(payload: TelemetryIn, session: DbSession) -> Any:
    """HTTP ingest path, mirroring the MQTT one.

    Exists so the pipeline can be exercised (and tested) without a broker.
    """
    result = get_ingest_service().process_telemetry(payload, session)
    session.commit()
    return result


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@app.get("/api/alerts", response_model=list[AlertOut], tags=["alerts"])
def list_alerts(
    session: DbSession,
    device_id: str | None = None,
    only_open: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[Alert]:
    query = select(Alert).order_by(Alert.triggered_at.desc()).limit(limit)
    if device_id:
        query = query.where(Alert.device_id == device_id)
    if only_open:
        query = query.where(Alert.resolved_at.is_(None))
    return list(session.scalars(query).all())


@app.post("/api/alerts/{alert_id}/acknowledge", response_model=AlertOut, tags=["alerts"])
def acknowledge_alert(alert_id: int, session: DbSession) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    alert.acknowledged = True
    session.commit()
    session.refresh(alert)
    return alert


@app.post("/api/alerts/{alert_id}/resolve", response_model=AlertOut, tags=["alerts"])
def resolve_alert(alert_id: int, session: DbSession) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    if alert.resolved_at is None:
        alert.resolved_at = utcnow()
    session.commit()
    session.refresh(alert)
    return alert


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


@app.get("/api/thresholds", response_model=list[ThresholdOut], tags=["config"])
def list_thresholds(session: DbSession) -> list[Threshold]:
    return list(session.scalars(select(Threshold).order_by(Threshold.key)).all())


@app.put("/api/thresholds", response_model=list[ThresholdOut], tags=["config"])
def update_thresholds(
    payload: ThresholdBulkUpdate, session: DbSession
) -> list[Threshold]:
    known = {row.key for row in session.scalars(select(Threshold)).all()}
    unknown = set(payload.thresholds) - known
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown threshold key(s): {', '.join(sorted(unknown))}",
        )
    for key, value in payload.thresholds.items():
        row = session.get(Threshold, key)
        if row is not None:
            row.value = value
            row.updated_at = utcnow()
    session.commit()
    return list(session.scalars(select(Threshold).order_by(Threshold.key)).all())


# ---------------------------------------------------------------------------
# Ad-hoc prediction
# ---------------------------------------------------------------------------


@app.post("/api/predict", response_model=PredictResponse, tags=["model"])
def predict(payload: PredictRequest) -> PredictResponse:
    from app.inference import compute_heat_index

    features = payload.model_dump()
    features["flame_detected"] = float(payload.flame_detected)
    features["heat_index_c"] = compute_heat_index(
        payload.temperature_c, payload.humidity_pct
    )
    status, probabilities = get_classifier().predict(features)
    return PredictResponse(
        status=status,  # type: ignore[arg-type]
        fire_probability=round(probabilities.get("FIRE", 0.0), 6),
        risk_score=compute_risk_score(probabilities),
        probabilities={k: round(v, 6) for k, v in probabilities.items()},
    )


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


@app.delete("/api/readings", tags=["system"])
def prune_readings(session: DbSession, older_than_days: int | None = None) -> dict:
    """Delete readings older than the retention window."""
    days = older_than_days or get_settings().retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # A DELETE always yields a CursorResult; the base Result type has no
    # rowcount, so narrow it rather than reaching through getattr.
    result = cast(
        "CursorResult[Any]",
        session.execute(delete(Reading).where(Reading.recorded_at < cutoff)),
    )
    session.commit()
    return {"deleted": int(result.rowcount or 0), "older_than_days": days}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket hijacking attempts.

    Browsers do not apply the same-origin policy or CORS preflight to
    WebSocket handshakes, so any page the user happens to visit could open a
    socket to a FireProtect running on their machine and read the live sensor
    feed. Checking Origin closes that.

    A request with no Origin header is not from a browser (curl, the ESP32, a
    test harness) and is allowed — the header cannot be forged by a browser,
    which is exactly what makes this check meaningful.
    """
    settings = get_settings()
    if not settings.websocket_strict_origin:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    if origin in settings.cors_origins:
        return True
    # Same-origin: the dashboard is served by this very app, so its Origin is
    # whatever host the user typed. Accept when it matches the Host header.
    host = websocket.headers.get("host")
    if host and origin.split("://", 1)[-1] == host:
        return True
    logger.warning("rejected websocket handshake from disallowed origin %r", origin)
    return False


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Live feed of readings, alerts and device state changes."""
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=1008, reason="origin not allowed")
        return

    await hub.connect(websocket)
    try:
        await websocket.send_json(
            {"type": "hello", "payload": {"version": VERSION, "protocol": 1}}
        )
        while True:
            # The client is not expected to send anything; this receive exists
            # to detect disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        await hub.disconnect(websocket)


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------
#
# Serving the built frontend from the API removes the whole class of problems
# that come from running it separately: no second dev server, no CORS, no port
# mismatch between the Vite proxy and the backend, and one URL to open. In
# production nginx can still front it (see frontend/nginx.conf); this is the
# zero-dependency path.
#
# Mounted LAST so it never shadows /api, /ws, /docs or /openapi.json.


class SpaStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html for client-side routes.

    A single-page app owns its routing, so a request for an unknown path is
    usually a deep link, not a missing file. Genuine 404s for assets still
    matter, so the fallback only applies to paths that do not look like files.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in Path(path).name:
                return await super().get_response("index.html", scope)
            raise


def _mount_dashboard(application: FastAPI) -> None:
    dist = get_settings().frontend_dist
    index = dist / "index.html"
    if not index.is_file():
        logger.warning(
            "dashboard build not found at %s - the API will run without a UI. "
            "Build it with `cd frontend && npm install && npm run build`.",
            dist,
        )
        return
    # html=True makes StaticFiles resolve "/" to index.html.
    application.mount(
        "/", SpaStaticFiles(directory=str(dist), html=True), name="dashboard"
    )
    logger.info("serving dashboard from %s", dist)


_mount_dashboard(app)
