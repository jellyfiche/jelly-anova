"""Diagnostics: what helps with a bug report, with every login, id and name removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .runtime import OvenRuntime

# Removed wherever they appear, however deep. The summary below is also picked item by item, so
# a new field on the oven state or the store cannot reach the output by accident.
TO_REDACT = {
    "refresh_token",
    "personal_token",
    "api_key",
    "email",
    "password",
    "username",
    "uid",
    "user_id",
    "account_id",
    "id_token",
    "access_token",
    "token",
    "cooker_id",
    "cookerId",
    "device_id",
    "deviceId",
    "id",
    "cook_id",
    "cookId",
    "cookable_id",
    "cookableId",
    "wifi_name",
    "wifiName",
    "ssid",
    "processed_command_ids",
    "processedCommandIds",
    "command_ids",
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the diagnostics for the oven."""
    runtime: OvenRuntime = entry.runtime_data
    state = runtime.state
    saved = runtime.saved_snapshot()
    oven = {
        "connected": runtime.connected,
        "available": runtime.available,
        "mode": state.mode if state else None,
        "firmware": state.firmware if state else None,
        "online": state.online if state else None,
        "last_report": _iso(state.updated) if state else None,
        "cook_saved": saved["cook_id"] is not None,
        "saved_stop_at": saved["stop_at"],
        "saved_max_minutes": saved["max_minutes"],
        "total_cook_hours": runtime.total_cook_hours,
    }
    return {
        # Key names only: redaction by name cannot catch a secret under a name nobody listed.
        "entry": {
            "data_keys": sorted(entry.data),
            "option_keys": sorted(entry.options),
        },
        "oven": async_redact_data(oven, TO_REDACT),
        "store": async_redact_data(saved, TO_REDACT),
    }
