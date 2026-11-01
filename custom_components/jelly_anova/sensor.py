"""Sensors: everything the oven reports as a number or a word.

A value the oven did not send is None (unknown), never 0.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricPotential, UnitOfFrequency, UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import JellyAnovaEntity
from .runtime import OvenRuntime
from .state import OvenState


@dataclass(frozen=True, kw_only=True)
class JellyAnovaSensorDescription(SensorEntityDescription):
    """`value_fn` reads a report; `runtime_fn` reads the runtime (Home Assistant's own count)."""

    value_fn: Callable[[OvenState], Any] | None = None
    runtime_fn: Callable[[OvenRuntime], Any] | None = None


def _cook(state: OvenState, name: str) -> Any:
    return getattr(state.cook, name) if state.cook is not None else None


def _active_stage(state: OvenState) -> int | None:
    """The stage number from 1. The hidden end stage is not counted, so while it runs the
    last real stage shows (its raw index would equal the stage count)."""
    cook = state.cook
    if cook is None or cook.stage_index is None or cook.stage_count < 1:
        return None
    return min(cook.stage_index + 1, cook.stage_count)


def _stage_value(state: OvenState, value: Any) -> Any:
    """A value taken from the running stage; nothing while the hidden end stage runs."""
    if state.cook is not None and state.cook.in_end_stage:
        return None
    return value


def _minutes(seconds: float, up: bool = False) -> int:
    """Whole minutes, as seconds, so the sensor changes once a minute (the recorder)."""
    return int(math.ceil(seconds / 60) if up else round(seconds / 60)) * 60


def _cook_time(state: OvenState) -> int | None:
    started = _cook(state, "started")
    if started is None:
        return None
    return _minutes(max(0.0, (state.updated - started).total_seconds()))


def _timer(state: OvenState) -> int | None:
    """Time left as of the report, in whole minutes (seconds). Counted down while running,
    0 when completed, the full time while waiting (idle node with `initial`). Any other idle
    means it ran and ended: unknown. None while the hidden end stage runs (its timer is not the user's)."""
    cook = state.cook
    if cook is None or cook.in_end_stage or cook.timer_initial is None or cook.timer_mode is None:
        return None
    if cook.timer_mode == "running":
        if cook.timer_started is None:
            return None
        left = cook.timer_initial - (state.updated - cook.timer_started).total_seconds()
        return _minutes(max(0.0, left), up=True)
    if cook.timer_mode == "completed":
        return 0
    if cook.timer_mode == "idle":
        return cook.timer_initial if cook.timer_waiting else None
    return cook.timer_initial


def _watts(name: str) -> Callable[[OvenState], int | None]:
    return lambda state: state.watts.get(name)


def _hours(name: str) -> Callable[[OvenState], float | None]:
    return lambda state: state.usage_hours.get(name)


def _fan(name: str) -> Callable[[OvenState], str | None]:
    return lambda state: state.fan_speeds.get(name)


def _food(name: str, scale: float = 1) -> Callable[[OvenState], Any]:
    def read(state: OvenState) -> Any:
        if not state.food:
            return None
        value = state.food[0].get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(value * scale, 1) if scale != 1 else value

    return read


_TEMPERATURE = {
    "device_class": SensorDeviceClass.TEMPERATURE,
    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 0,
}
_WATTS = {
    "device_class": SensorDeviceClass.POWER,
    "native_unit_of_measurement": UnitOfPower.WATT,
    "state_class": SensorStateClass.MEASUREMENT,
}
_SECONDS = {
    "device_class": SensorDeviceClass.DURATION,
    "native_unit_of_measurement": UnitOfTime.SECONDS,
}
_USAGE = {
    "device_class": SensorDeviceClass.DURATION,
    "native_unit_of_measurement": UnitOfTime.HOURS,
    "state_class": SensorStateClass.TOTAL_INCREASING,
    "suggested_display_precision": 1,
    "entity_category": EntityCategory.DIAGNOSTIC,
}
_DIAGNOSTIC_OFF = {"entity_category": EntityCategory.DIAGNOSTIC, "entity_registry_enabled_default": False}


def _d(key: str, value_fn: Callable[[OvenState], Any], **kwargs: Any) -> JellyAnovaSensorDescription:
    return JellyAnovaSensorDescription(key=key, translation_key=key, value_fn=value_fn, **kwargs)


SENSORS: tuple[JellyAnovaSensorDescription, ...] = (
    # Cook
    _d("mode", lambda s: s.mode),
    _d("active_stage", _active_stage),
    _d("stages_count", lambda s: s.cook.stage_count if s.cook is not None else None),
    _d("cook_time", _cook_time, **_SECONDS),
    _d("timer", _timer, **_SECONDS),
    _d("timer_initial", lambda s: _stage_value(s, _cook(s, "timer_initial")), **_SECONDS),
    _d("timer_mode", lambda s: _stage_value(s, _cook(s, "timer_mode"))),
    _d("cook_origin", lambda s: _cook(s, "origin")),
    # Temperature
    _d("temperature", lambda s: s.dry_c, **_TEMPERATURE),
    _d("wet_temperature", lambda s: s.wet_c, **_TEMPERATURE),
    _d("top_temperature", lambda s: s.dry_top_c, **_TEMPERATURE),
    _d("bottom_temperature", lambda s: s.dry_bottom_c, **_TEMPERATURE),
    _d("target_temperature", lambda s: _stage_value(s, s.target_c), **_TEMPERATURE),
    _d("temperature_probe", lambda s: s.probe_c, **_TEMPERATURE),
    _d("target_temperature_probe", lambda s: _stage_value(s, s.target_probe_c), **_TEMPERATURE),
    # Steam
    _d("steam_generator_mode", lambda s: s.steam_mode),
    _d(
        "relative_humidity",
        lambda s: s.humidity,
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _d(
        "target_humidity",
        lambda s: _stage_value(s, s.target_humidity),
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
    ),
    _d(
        "steam_percentage",
        lambda s: s.steam_percent,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _d("target_steam_percentage", lambda s: _stage_value(s, s.target_steam_percent), native_unit_of_measurement=PERCENTAGE),
    _d("boiler_watts", _watts("boiler"), **_WATTS),
    _d("evaporator_watts", _watts("evaporator"), **_WATTS),
    _d("boiler_temperature", lambda s: s.boiler_c, **_TEMPERATURE),
    _d("evaporator_temperature", lambda s: s.evaporator_c, **_TEMPERATURE),
    # Power
    _d("top_watts", _watts("top"), **_WATTS),
    _d("bottom_watts", _watts("bottom"), **_WATTS),
    _d("rear_watts", _watts("rear"), **_WATTS),
    _d("fan_speed", lambda s: s.fan_speed),
    _d("vent_state", lambda s: s.vent_state),
    # Maintenance
    _d("top_element_hours", _hours("top"), **_USAGE),
    _d("bottom_element_hours", _hours("bottom"), **_USAGE),
    _d("rear_element_hours", _hours("rear"), **_USAGE),
    _d("boiler_hours", _hours("boiler"), **_USAGE),
    _d("evaporator_hours", _hours("evaporator"), **_USAGE),
    _d("firmware", lambda s: s.firmware, entity_category=EntityCategory.DIAGNOSTIC),
    _d(
        "firmware_updated",
        lambda s: s.firmware_updated,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Home Assistant's own count of cooking time
    JellyAnovaSensorDescription(
        key="total_cook_hours",
        translation_key="total_cook_hours",
        runtime_fn=lambda runtime: round(runtime.total_cook_hours, 2),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    # Reported but rarely useful: present, off until switched on
    _d("exhaust_fan_speed", _fan("exhaust"), **_DIAGNOSTIC_OFF),
    _d("led_fan_speed", _fan("led"), **_DIAGNOSTIC_OFF),
    _d("power_board_fan", _fan("power_board"), **_DIAGNOSTIC_OFF),
    _d("display_fan_speed", _fan("display"), **_DIAGNOSTIC_OFF),
    _d("display_board_temperature", lambda s: s.display_board_c, **_TEMPERATURE, **_DIAGNOSTIC_OFF),
    _d("internal_line_faults", lambda s: s.dc12v_faults, state_class=SensorStateClass.TOTAL_INCREASING, **_DIAGNOSTIC_OFF),
    _d("boiler_pump_faults", lambda s: s.boiler_pump_faults, state_class=SensorStateClass.TOTAL_INCREASING, **_DIAGNOSTIC_OFF),
    _d("boiler_valve_faults", lambda s: s.boiler_valve_faults, state_class=SensorStateClass.TOTAL_INCREASING, **_DIAGNOSTIC_OFF),
    _d("hardware_version", lambda s: s.hardware, **_DIAGNOSTIC_OFF),
    _d(
        "mains_voltage",
        lambda s: s.mains_volts,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        **_DIAGNOSTIC_OFF,
    ),
    _d(
        "mains_frequency",
        lambda s: s.mains_hertz,
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        **_DIAGNOSTIC_OFF,
    ),
    _d("last_disconnected", lambda s: s.last_disconnected, device_class=SensorDeviceClass.TIMESTAMP, **_DIAGNOSTIC_OFF),
    _d("door_light_preference", lambda s: s.door_lamp_preference, **_DIAGNOSTIC_OFF),
    _d("food_category", _food("categoryId"), **_DIAGNOSTIC_OFF),
    _d("food_confidence", _food("confidence", 100), native_unit_of_measurement=PERCENTAGE, **_DIAGNOSTIC_OFF),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: OvenRuntime = entry.runtime_data
    async_add_entities(OvenSensor(runtime, entry.entry_id, d) for d in SENSORS)


class OvenSensor(JellyAnovaEntity, SensorEntity):
    entity_description: JellyAnovaSensorDescription

    @property
    def native_value(self) -> Any:
        description = self.entity_description
        if description.runtime_fn is not None:
            return description.runtime_fn(self.runtime)
        state = self.oven_state
        if state is None or description.value_fn is None:
            return None
        return description.value_fn(state)
