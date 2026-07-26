"""Tests for deployment-time configuration and demo mode.

These cover the settings paths that only ever run on a hosting platform, which
is exactly where a mistake is most expensive to discover.
"""

from __future__ import annotations

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
