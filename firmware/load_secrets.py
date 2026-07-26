"""PlatformIO pre-build script: inject Wi-Fi / MQTT settings as build flags.

Reads ``firmware/secrets.ini`` if present and turns each key into a -D define.
The file is git-ignored, so credentials never enter the repository. If it is
missing, safe placeholder defaults are used and a warning is printed - the
firmware still compiles, which keeps CI green without secrets.
"""

from __future__ import annotations

import configparser
from pathlib import Path

Import("env")  # type: ignore[name-defined]  # noqa: F821 - injected by PlatformIO

HERE = Path(__file__).resolve().parent
SECRETS = HERE / "secrets.ini"

DEFAULTS: dict[str, str] = {
    "wifi_ssid": "CHANGE_ME",
    "wifi_password": "CHANGE_ME",
    "mqtt_host": "192.168.1.100",
    "mqtt_port": "1883",
    "mqtt_username": "",
    "mqtt_password": "",
    "device_id": "esp32-01",
    "device_location": "Unassigned",
    "thingspeak_api_key": "",
}

#: Numeric flags must not be quoted in the generated -D define.
NUMERIC = {"mqtt_port"}

values = dict(DEFAULTS)

if SECRETS.exists():
    parser = configparser.ConfigParser()
    parser.read(SECRETS)
    if parser.has_section("secrets"):
        for key in DEFAULTS:
            if parser.has_option("secrets", key):
                values[key] = parser.get("secrets", key)
    print(f"[fireprotect] loaded build secrets from {SECRETS.name}")
else:
    print(
        "[fireprotect] WARNING: firmware/secrets.ini not found; building with "
        "placeholder credentials. Copy secrets.ini.example to secrets.ini and "
        "edit it before flashing real hardware."
    )

flags = []
for key, value in values.items():
    define = key.upper()
    flags.append(f"-D {define}={value}" if key in NUMERIC else f'-D {define}=\\"{value}\\"')

env.Append(CPPDEFINES=[])  # type: ignore[name-defined]  # noqa: F821
env.Append(BUILD_FLAGS=flags)  # type: ignore[name-defined]  # noqa: F821
