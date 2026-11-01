"""The actions: start_cook, start_custom_cook, stop_cook, update_cook, next_stage, update_stages.

They are registered when Home Assistant loads the integration (async_setup), so they exist with no
oven set up and then say so in plain words.

start_cook takes the new short names (temperature_f, probe_f, minutes ...) and every field name the
old andr83 integration had, so old scripts convert by renaming one action. Old meanings:
`target_humidity` was the steam percentage (old scripts say "50% steam"), or the humidity when
`sous_vide` is on; `timer` was a duration; `heating_top/bottom/rear` were switches; `device_id` is
accepted and ignored because there is one oven.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .cook import ELEMENTS, VENTS, CookError, CookRequest
from .errors import plain_errors
from .runtime import OvenRuntime

TIMER_MODES = ["Immediately", "When Preheated", "Manually", "When Food Detected", "When Food Removed"]
NOT_SET_UP = (
    "The Anova oven is not set up in Home Assistant, so this action cannot run. "
    "Add the Jelly Anova integration first."
)

_NUMBER = vol.Coerce(float)
_WHOLE = vol.Coerce(int)
_ID = vol.Any(None, cv.match_all)

START_COOK_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): _ID,  # accepted and ignored: there is one oven
        # New names
        vol.Optional("temperature_f"): _NUMBER,
        vol.Optional("temperature_c"): _NUMBER,
        vol.Optional("probe_f"): _NUMBER,
        vol.Optional("probe_c"): _NUMBER,
        vol.Optional("minutes"): _NUMBER,
        vol.Optional("elements"): vol.All(cv.ensure_list, [vol.In(ELEMENTS)]),
        vol.Optional("steam_percent"): _NUMBER,
        vol.Optional("humidity"): _NUMBER,
        vol.Optional("sous_vide"): cv.boolean,
        vol.Optional("timer_mode"): vol.In(TIMER_MODES),
        vol.Optional("fan"): _NUMBER,
        vol.Optional("vent"): vol.In(VENTS),
        vol.Optional("rack"): _WHOLE,
        vol.Optional("max_minutes"): _NUMBER,
        # andr83's names
        vol.Optional("target_temperature_celsius"): _NUMBER,
        vol.Optional("target_temperature_fahrenheit"): _NUMBER,
        vol.Optional("temperature_probe_celsius"): _NUMBER,
        vol.Optional("temperature_probe_fahrenheit"): _NUMBER,
        vol.Optional("target_humidity"): _NUMBER,
        vol.Optional("heating_top"): cv.boolean,
        vol.Optional("heating_bottom"): cv.boolean,
        vol.Optional("heating_rear"): cv.boolean,
        vol.Optional("timer"): cv.time_period,
    }
)

_STAGES = vol.Any([dict], cv.string)
START_CUSTOM_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): _ID,
        vol.Required("config"): _STAGES,
        vol.Optional("max_minutes"): _NUMBER,
    }
)
STOP_SCHEMA = vol.Schema({vol.Optional("device_id"): _ID, vol.Optional("cook_id"): cv.string})
UPDATE_COOK_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): _ID,
        vol.Optional("temperature_f"): _NUMBER,
        vol.Optional("temperature_c"): _NUMBER,
        vol.Optional("steam_percent"): _NUMBER,
        vol.Optional("humidity"): _NUMBER,
        vol.Optional("fan"): _NUMBER,
        vol.Optional("vent"): vol.In(VENTS),
        vol.Optional("timer"): cv.time_period,
        vol.Optional("elements"): vol.All(cv.ensure_list, [vol.In(ELEMENTS)]),
        vol.Optional("start_timer"): cv.boolean,
    }
)
NEXT_STAGE_SCHEMA = vol.Schema({vol.Optional("device_id"): _ID, vol.Required("stage_id"): cv.string})
UPDATE_STAGES_SCHEMA = vol.Schema({vol.Optional("device_id"): _ID, vol.Required("config"): _STAGES})


def _one(data: dict, what: str, *names: str) -> Any:
    """The value given under one of several names. Two names for one value is an error."""
    given = [name for name in names if data.get(name) is not None]
    if len(given) > 1:
        raise CookError(f"Give the {what} once: {given[0]} and {given[1]} are the same thing.")
    return data[given[0]] if given else None


def _minutes(data: dict) -> float | None:
    timer: timedelta | None = data.get("timer")
    minutes = data.get("minutes")
    if timer is None:
        return minutes
    if minutes is not None:
        raise CookError("Give the timer as minutes or as a timer duration, not both.")
    seconds = timer.total_seconds()
    if seconds == 0:
        return None  # an empty duration means no timer
    if seconds % 60:
        raise CookError("The timer must be a whole number of minutes.")
    return int(seconds // 60)


def _elements(data: dict) -> frozenset[str] | None:
    old = {name: data.get(f"heating_{name}") for name in ELEMENTS}
    given_old = any(value is not None for value in old.values())
    if data.get("elements") is not None:
        if given_old:
            raise CookError("Give the heating elements as a list or as heating_top, heating_bottom and heating_rear, not both.")
        return frozenset(data["elements"])
    if given_old:
        return frozenset(name for name, on in old.items() if on)
    return None


def cook_request(data: dict) -> CookRequest:
    """The start_cook fields, in the new or the old names, as a CookRequest."""
    sous_vide = bool(data.get("sous_vide", False))
    old_humidity = data.get("target_humidity")
    fields: dict[str, Any] = {
        "temperature_f": _one(data, "oven temperature", "temperature_f", "target_temperature_fahrenheit"),
        "temperature_c": _one(data, "oven temperature", "temperature_c", "target_temperature_celsius"),
        "probe_f": _one(data, "probe temperature", "probe_f", "temperature_probe_fahrenheit"),
        "probe_c": _one(data, "probe temperature", "probe_c", "temperature_probe_celsius"),
        "minutes": _minutes(data),
        "elements": _elements(data),
        "steam_percent": data.get("steam_percent"),
        "humidity": data.get("humidity"),
        "sous_vide": sous_vide,
        "timer_mode": data.get("timer_mode"),
        "fan": data.get("fan"),
        "vent": data.get("vent"),
        "rack": data.get("rack"),
        "max_minutes": data.get("max_minutes"),
    }
    if old_humidity is not None:
        target = "humidity" if sous_vide else "steam_percent"
        if fields[target] is not None:
            raise CookError(f"Give the {target.replace('_', ' ')} once: {target} and target_humidity are the same thing.")
        fields[target] = old_humidity
    return CookRequest(**{name: value for name, value in fields.items() if value is not None or name == "sous_vide"})


def parse_stages(config: Any) -> list[dict]:
    """Stages as a list, or as JSON text (a list, or an object with a "stages" list)."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError as err:
            line = getattr(err, "lineno", None)
            where = f" (near line {line})" if line else ""
            raise CookError(f"The stages are not valid JSON{where}. Paste the stages list as JSON text.") from None
    if isinstance(config, dict):
        config = config.get("stages")
    if not isinstance(config, list) or not config or not all(isinstance(stage, dict) for stage in config):
        raise CookError("Give the stages as a JSON list with at least one stage.")
    return config


def _runtime(hass: HomeAssistant) -> OvenRuntime:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    raise ServiceValidationError(NOT_SET_UP)


async def _start_cook(call: ServiceCall) -> dict:
    runtime = _runtime(call.hass)
    with plain_errors():
        cook_id = await runtime.start(cook_request(call.data))
    return {"cook_id": cook_id}


async def _start_custom_cook(call: ServiceCall) -> dict:
    runtime = _runtime(call.hass)
    with plain_errors():
        cook_id = await runtime.start_custom(parse_stages(call.data["config"]), call.data.get("max_minutes"))
    return {"cook_id": cook_id}


async def _stop_cook(call: ServiceCall) -> None:
    runtime = _runtime(call.hass)
    with plain_errors():
        await runtime.stop(call.data.get("cook_id"))


async def _update_cook(call: ServiceCall) -> None:
    runtime = _runtime(call.hass)
    data = call.data
    changes: dict[str, Any] = {
        name: data[name]
        for name in ("temperature_f", "temperature_c", "steam_percent", "humidity", "fan", "vent", "elements")
        if data.get(name) is not None
    }
    if data.get("timer") is not None:
        changes["timer_seconds"] = data["timer"].total_seconds()
    if data.get("start_timer"):
        changes["start_timer"] = True
    with plain_errors():
        if not changes:
            raise CookError("Give at least one thing to change.")
        await runtime.update(changes)


async def _next_stage(call: ServiceCall) -> None:
    runtime = _runtime(call.hass)
    with plain_errors():
        await runtime.next_stage(call.data["stage_id"])


async def _update_stages(call: ServiceCall) -> None:
    runtime = _runtime(call.hass)
    with plain_errors():
        await runtime.update_stages(parse_stages(call.data["config"]))


RESPONSES = {
    "start_cook": SupportsResponse.OPTIONAL,
    "start_custom_cook": SupportsResponse.OPTIONAL,
}
_ACTIONS = {
    "start_cook": (_start_cook, START_COOK_SCHEMA),
    "start_custom_cook": (_start_custom_cook, START_CUSTOM_SCHEMA),
    "stop_cook": (_stop_cook, STOP_SCHEMA),
    "update_cook": (_update_cook, UPDATE_COOK_SCHEMA),
    "next_stage": (_next_stage, NEXT_STAGE_SCHEMA),
    "update_stages": (_update_stages, UPDATE_STAGES_SCHEMA),
}


def async_setup_services(hass: HomeAssistant) -> None:
    for name, (handler, schema) in _ACTIONS.items():
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=RESPONSES.get(name, SupportsResponse.NONE)
        )
