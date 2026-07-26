"""Tests for deployment-time configuration and demo mode.

These cover the settings paths that only ever run on a hosting platform, which
is exactly where a mistake is most expensive to discover.
"""

from __future__ import annotations

import re

import pytest

from app.config import Settings

# --------------------------------------------------------------------------
# CORS_ORIGINS parsing
# --------------------------------------------------------------------------


def test_cors_origins_accepts_a_bare_url(monkeypatch):
    """Hosting dashboards give you a plain text box, not a JSON editor.

    Without NoDecode, pydantic-settings JSON-decodes this straight out of the
    environment and the app dies at boot with `SettingsError`.
    """
    monkeypatch.setenv("CORS_ORIGINS", "https://fireprotect.vercel.app")
    assert Settings().cors_origins == ["https://fireprotect.vercel.app"]


def test_cors_origins_accepts_comma_separated(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://a.vercel.app, http://localhost:5173")
    assert Settings().cors_origins == [
        "https://a.vercel.app",
        "http://localhost:5173",
    ]


def test_cors_origins_still_accepts_json(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://b.vercel.app"]')
    assert Settings().cors_origins == ["https://b.vercel.app"]


def test_cors_origins_empty_string_means_none(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "")
    assert Settings().cors_origins == []


def test_cors_origins_rejects_malformed_json(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["unterminated')
    with pytest.raises(Exception, match=r"not valid|CORS_ORIGINS"):
        Settings()


def test_cors_origins_defaults_to_localhost_only():
    assert all("localhost" in origin for origin in Settings().cors_origins)


# --------------------------------------------------------------------------
# Demo mode
# --------------------------------------------------------------------------


def test_demo_mode_is_off_by_default():
    """A real deployment must never mix synthetic readings into its history."""
    assert Settings().demo_mode is False


def test_demo_mode_enabled_by_env(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    assert Settings().demo_mode is True


def test_demo_driver_loads_the_shared_simulator():
    """Demo telemetry must come from the same physics as the simulator."""
    from app.demo import DemoDriver

    driver = DemoDriver()
    assert driver.available, "simulator/virtual_device.py could not be imported"


def test_demo_driver_cycle_covers_calm_and_fire():
    """A visitor arriving at any moment should see something worth looking at."""
    from app.demo import DemoDriver

    scenarios = {name for name, _ in DemoDriver.CYCLE}
    assert "normal" in scenarios
    assert scenarios & {"flashover", "smoldering"}


def test_demo_driver_degrades_when_simulator_missing(monkeypatch):
    """A missing simulator must disable demo data, never crash the app."""
    from app import demo as demo_module

    monkeypatch.setattr(demo_module, "_load_virtual_device", lambda: None)
    driver = demo_module.DemoDriver()
    assert driver.available is False


@pytest.mark.asyncio
async def test_demo_driver_start_is_a_noop_when_unavailable(monkeypatch):
    from app import demo as demo_module

    monkeypatch.setattr(demo_module, "_load_virtual_device", lambda: None)
    driver = demo_module.DemoDriver()
    await driver.start()
    await driver.stop()  # must not raise


# --------------------------------------------------------------------------
# Frontend build output — required for the backend to serve the dashboard
# --------------------------------------------------------------------------


def test_committed_dashboard_build_exists():
    from app.config import REPO_ROOT

    index = REPO_ROOT / "frontend" / "dist" / "index.html"
    assert index.is_file(), "run `npm run build` in frontend/ and commit dist/"
    assert "<div id=\"root\">" in index.read_text(encoding="utf-8")


def test_dashboard_references_only_files_that_exist():
    """Every asset index.html asks for must actually be in dist/.

    Regression test for a live blank-white-page outage. `index.html` pointed at
    a content-hashed bundle that had not been uploaded, so the browser 404'd on
    the script and rendered nothing at all. Assets now have stable names, but
    the invariant worth enforcing is the general one: the entry document must
    never reference a file the deployment does not contain.
    """
    from app.config import REPO_ROOT

    dist = REPO_ROOT / "frontend" / "dist"
    html = (dist / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', html))
    assert referenced, "index.html references no assets at all - build is broken"

    missing = [ref for ref in referenced if not (dist / ref.lstrip("/")).is_file()]
    assert not missing, f"index.html references files missing from dist/: {missing}"


def test_dashboard_has_a_visible_failure_mode():
    """A broken deploy must say so, not render a blank white page.

    The fallback is CSS-only (`#root:empty`) precisely so it still works when
    the stylesheet or the bundle is the thing that failed.
    """
    from app.config import REPO_ROOT

    html = (REPO_ROOT / "frontend" / "dist" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "boot-fallback" in html
    assert "#root:empty" in html


# --------------------------------------------------------------------------
# Timestamps — every one must carry an explicit UTC offset
# --------------------------------------------------------------------------
#
# Regression for a live bug: SQLite ignores `DateTime(timezone=True)`, so
# timestamps came back naive and serialised without an offset. JavaScript
# parses an offsetless date-time as LOCAL time, so a device that reported two
# seconds ago rendered as "seen 6h ago" for a reader in IST, and the telemetry
# charts were plotted against an axis wrong by the reader's UTC offset.


def _offset_bearing(value: str) -> bool:
    """True if an ISO-8601 string states its timezone."""
    return value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value) is not None


def test_device_timestamps_carry_an_offset(client, sample_telemetry):
    client.post("/api/telemetry", json=sample_telemetry)
    device = client.get("/api/devices").json()[0]
    for field in ("first_seen", "last_seen"):
        assert _offset_bearing(device[field]), (
            f"{field}={device[field]!r} has no timezone; browsers will read it "
            f"as local time and show it shifted by the viewer's UTC offset"
        )


def test_reading_timestamps_carry_an_offset(client, sample_telemetry):
    client.post("/api/telemetry", json=sample_telemetry)
    reading = client.get("/api/readings").json()[0]
    assert _offset_bearing(reading["recorded_at"])


def test_alert_timestamps_carry_an_offset(client, fire_telemetry):
    for _ in range(4):
        client.post("/api/telemetry", json=fire_telemetry)
    alerts = client.get("/api/alerts").json()
    assert alerts, "expected a FIRE alert from repeated fire telemetry"
    assert _offset_bearing(alerts[0]["triggered_at"])


def test_websocket_payload_matches_the_rest_representation(client, sample_telemetry):
    """The socket builds dicts by hand and never touches Pydantic.

    If it emitted naive timestamps while REST emitted aware ones, a live chart
    would jump by the viewer's UTC offset the instant an update arrived.
    """
    from app.db import Device, Reading, session_scope
    from app.ingest import _device_to_dict, _reading_to_dict

    client.post("/api/telemetry", json=sample_telemetry)
    with session_scope() as session:
        reading = session.query(Reading).first()
        device = session.query(Device).first()
        assert reading is not None and device is not None
        assert _offset_bearing(_reading_to_dict(reading)["recorded_at"])
        assert _offset_bearing(_device_to_dict(device)["last_seen"])


def test_a_fresh_reading_is_not_reported_as_hours_old(client, sample_telemetry):
    """The user-visible symptom, asserted directly.

    Parsing the serialised timestamp the way a browser does must place it
    within seconds of now — not the viewer's UTC offset into the past.
    """
    from datetime import datetime, timezone

    client.post("/api/telemetry", json=sample_telemetry)
    last_seen = client.get("/api/devices").json()[0]["last_seen"]
    # `fromisoformat` only learned to accept a trailing "Z" in Python 3.11.
    # Browsers have always accepted it, so the wire format is right; this is
    # purely so the test runs on 3.10 as well.
    parsed = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
    age_s = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert age_s < 60, f"a just-created device appears {age_s / 3600:.1f} hours old"


# --------------------------------------------------------------------------
# Container contents — what the Dockerfile must ship for the app to work
# --------------------------------------------------------------------------


def _dockerfile() -> str:
    from app.config import REPO_ROOT

    return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "required",
    [
        "backend/requirements.txt",
        "backend/app",
        "ml/random_forest.joblib",
        "ml/decision_tree.joblib",
        "ml/feature_columns.json",
        "frontend/dist",
        # Demo mode imports the simulator, which in turn imports the dataset
        # module for its sensor curves. Omitting either makes DEMO_MODE fail
        # silently at boot and leaves every visitor looking at an empty
        # dashboard -- which is exactly what shipped once.
        "simulator/virtual_device.py",
        "ml/generate_dataset.py",
    ],
)
def test_dockerfile_copies_every_runtime_dependency(required: str):
    assert required in _dockerfile(), (
        f"Dockerfile does not COPY {required}; the container will be missing it"
    )


def test_simulator_import_chain_is_satisfiable():
    """Assert the *reason* generate_dataset.py must ship, not just that it does.

    If virtual_device.py ever stops importing from ml/, this test still passes
    -- but if it starts importing something new, this is where that shows up.
    """
    from app.config import REPO_ROOT

    source = (REPO_ROOT / "simulator" / "virtual_device.py").read_text(
        encoding="utf-8"
    )
    dockerfile = _dockerfile()
    for module in re.findall(r"^from (\w+) import", source, re.M):
        candidate = REPO_ROOT / "ml" / f"{module}.py"
        if candidate.is_file():
            assert f"ml/{module}.py" in dockerfile, (
                f"virtual_device.py imports ml/{module}.py but the Dockerfile "
                f"does not copy it"
            )


def test_sklearn_pin_matches_the_trained_models():
    """joblib artefacts are not portable across scikit-learn minor versions.

    A loose pin let the container install 1.9 for models fitted under 1.7,
    which scikit-learn warns "might lead to breaking code or invalid results".
    """
    from app.config import REPO_ROOT

    requirements = (REPO_ROOT / "backend" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    line = next(
        row for row in requirements.splitlines() if row.startswith("scikit-learn")
    )
    import sklearn

    major, minor, *_ = sklearn.__version__.split(".")
    assert f"<{major}.{int(minor) + 1}" in line, (
        f"scikit-learn is pinned as {line!r}, which permits a minor version "
        f"other than the installed {sklearn.__version__} the models were "
        f"trained with"
    )
