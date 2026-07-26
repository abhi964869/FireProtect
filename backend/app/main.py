"""FastAPI application: REST endpoints, WebSocket, lifespan wiring."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.auth import (
    CurrentUser,
    create_token,
    find_user_by_email,
    hash_password,
    normalise_email,
    verify_password,
)
from app.config import get_settings
from app.db import (
    Alert,
    Device,
    Reading,
    TemperatureEvent,
    Threshold,
    User,
    get_db,
    init_db,
    utcnow,
)
from app.demo import DemoDriver
from app.inference import compute_risk_score, get_classifier
from app.ingest import get_ingest_service
from app.mqtt_client import get_subscriber
from app.notify import get_notifier
from app.schemas import (
    AlertOut,
    CameraTelemetryIn,
    ClaimDeviceRequest,
    DeviceDetail,
    DeviceOut,
    EmergencyContactUpdate,
    HealthOut,
    LoginRequest,
    NotificationStatusOut,
    PredictRequest,
    PredictResponse,
    ReadingOut,
    RegisterRequest,
    StatsOut,
    TelemetryIn,
    TemperatureEventOut,
    ThresholdBulkUpdate,
    ThresholdOut,
    TokenResponse,
    UserOut,
)
from app.thingspeak import get_mirror
from app.ws import hub

logger = logging.getLogger(__name__)

VERSION = "1.1.0"

#: A valid bcrypt digest of a value nobody can supply. Login verifies against
#: this when the address is unknown, so the endpoint burns the same ~100 ms
#: either way and cannot be timed to enumerate accounts.
_DUMMY_HASH = (
    "$2b$12$C6UzMDM.H6dfI/f/IKcEe.EPRQhkQGeNVWMEbLBDwSCPuXVBiZ.Wm"
)

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

@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a clean 422 even when the rejected value cannot be JSON-encoded.

    FastAPI's default handler echoes the offending input back in the error
    body. That is helpful right up until the input is NaN or Infinity, which
    ``json.dumps`` refuses to serialise — at which point a *malformed request*
    turns into a 500 from inside the error handler itself. A device sending a
    failed DHT22 read as NaN is exactly the case this project has to survive,
    so the inputs are stripped and the field paths kept.
    """
    errors = [
        {
            "loc": error.get("loc", ()),
            "msg": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


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
# Accounts
# ---------------------------------------------------------------------------


@app.post(
    "/api/auth/register",
    response_model=TokenResponse,
    status_code=201,
    tags=["auth"],
)
def register(payload: RegisterRequest, session: DbSession) -> TokenResponse:
    if not get_settings().allow_registration:
        raise HTTPException(
            status_code=403, detail="Registration is closed on this instance."
        )
    if find_user_by_email(session, payload.email) is not None:
        # Registration genuinely cannot hide account existence — the address is
        # either free or it is not. Login is where the oracle would matter, and
        # that endpoint gives nothing away.
        raise HTTPException(
            status_code=409, detail="An account with that email already exists."
        )
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    user = User(
        email=normalise_email(payload.email),
        password_hash=password_hash,
        display_name=payload.display_name.strip(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    token, expires_in = create_token(user.id)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@app.post("/api/auth/login", response_model=TokenResponse, tags=["auth"])
def login(payload: LoginRequest, session: DbSession) -> TokenResponse:
    user = find_user_by_email(session, payload.email)
    # Identical response and identical work for "no such user" and "wrong
    # password". Returning early on an unknown address would also leak account
    # existence through response timing, so the hash is verified either way.
    reference = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = verify_password(payload.password, reference)
    if user is None or not password_ok:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    token, expires_in = create_token(user.id)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@app.get("/api/auth/me", response_model=UserOut, tags=["auth"])
def current_user(user: CurrentUser) -> User:
    return user


@app.put("/api/auth/emergency-contact", response_model=UserOut, tags=["auth"])
def update_emergency_contact(
    payload: EmergencyContactUpdate, user: CurrentUser, session: DbSession
) -> User:
    """Set who gets emailed, and at what temperature."""
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if value is not None or field == "emergency_email":
            setattr(user, field, value if value is not None else "")
    session.commit()
    session.refresh(user)
    return user


@app.get(
    "/api/auth/notification-status",
    response_model=NotificationStatusOut,
    tags=["auth"],
)
def notification_status(user: CurrentUser) -> NotificationStatusOut:
    """Whether an emergency email would actually be delivered right now.

    A settings page that accepts an address and says nothing else lets someone
    believe they are protected when the server has no mail transport at all.
    """
    notifier = get_notifier()
    return NotificationStatusOut(
        transport=notifier.transport,  # type: ignore[arg-type]
        configured=notifier.configured,
        contact_set=bool(user.emergency_email) and user.notifications_enabled,
    )


@app.get(
    "/api/temperature-events",
    response_model=list[TemperatureEventOut],
    tags=["history"],
)
def temperature_events(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TemperatureEvent]:
    """Every high-temperature excursion recorded for this account."""
    return list(
        session.scalars(
            select(TemperatureEvent)
            .where(TemperatureEvent.user_id == user.id)
            .order_by(TemperatureEvent.started_at.desc())
            .limit(limit)
        ).all()
    )


@app.post("/api/devices/claim", response_model=DeviceOut, tags=["devices"])
def claim_device(
    payload: ClaimDeviceRequest, user: CurrentUser, session: DbSession
) -> Device:
    """Take ownership of a node, so its emergencies reach your contact.

    First claim wins. Re-claiming something another account already owns is
    refused rather than silently transferred: quietly redirecting someone
    else's fire alarm to your inbox is exactly the attack this prevents.
    """
    device = session.get(Device, payload.device_id)
    if device is None:
        raise HTTPException(
            status_code=404, detail=f"device {payload.device_id!r} not found"
        )
    if device.owner_id is not None and device.owner_id != user.id:
        raise HTTPException(
            status_code=409, detail="That device is already claimed by another account."
        )
    device.owner_id = user.id
    session.commit()
    session.refresh(device)
    return device


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


@app.post("/api/camera/telemetry", response_model=ReadingOut, tags=["readings"])
def ingest_camera_telemetry(payload: CameraTelemetryIn, session: DbSession) -> Any:
    """Ingest one analysed frame from a browser camera node.

    Separate from ``/api/telemetry`` rather than a flag on it, because the two
    carry disjoint measurements and are scored by different models. One
    endpoint accepting either shape would need a discriminated union whose only
    purpose is to be immediately split apart again.

    The client sends three floats derived from the video on-device; no image
    data is transmitted or stored.
    """
    result = get_ingest_service().process_camera_telemetry(payload, session)
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
