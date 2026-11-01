"""Binary sensors. A flag the oven did not send is None (unknown), never off."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import JellyAnovaEntity
from .runtime import OvenRuntime
from .state import OvenState


@dataclass(frozen=True, kw_only=True)
class JellyAnovaBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[OvenState], bool | None]


def _failed(name: str) -> Callable[[OvenState], bool | None]:
    return lambda state: state.failed.get(name)


_PROBLEM = BinarySensorDeviceClass.PROBLEM
_DIAGNOSTIC = {"entity_category": EntityCategory.DIAGNOSTIC}
_RARE = {"entity_category": EntityCategory.DIAGNOSTIC, "entity_registry_enabled_default": False}


def _d(key: str, value_fn: Callable[[OvenState], bool | None], **kwargs) -> JellyAnovaBinaryDescription:
    return JellyAnovaBinaryDescription(key=key, translation_key=key, value_fn=value_fn, **kwargs)


BINARY_SENSORS: tuple[JellyAnovaBinaryDescription, ...] = (
    _d("cooking", lambda s: (s.mode == "cook") if s.mode is not None else None),
    # "Door closed" is on when the door is closed.
    _d("door_closed", lambda s: s.door_closed),
    _d("probe_connected", lambda s: s.probe_connected, device_class=BinarySensorDeviceClass.CONNECTIVITY),
    _d("vent_closed", lambda s: s.vent_closed),
    _d("water_low", lambda s: s.water_low, device_class=_PROBLEM),
    _d("water_tank_empty", lambda s: s.water_empty, device_class=_PROBLEM),
    _d("water_tank_removed", lambda s: s.water_removed),
    _d("waste_tank_full", lambda s: s.waste_full, device_class=_PROBLEM),
    _d("waste_tank_removed", lambda s: s.waste_removed),
    # cavityLamp.on only. doorLamp.on is unreliable; the door light switch is a separate entity.
    _d("lamp_on", lambda s: s.lamp_on, device_class=BinarySensorDeviceClass.LIGHT),
    _d("sous_vide", lambda s: (None if s.cook.in_end_stage else s.cook.sous_vide) if s.cook is not None else None),
    _d("oven_empty", lambda s: s.oven_empty),
    _d("online", lambda s: s.online, device_class=BinarySensorDeviceClass.CONNECTIVITY, **_DIAGNOSTIC),
    _d("descale_needed", lambda s: s.descale_needed, device_class=_PROBLEM),
    _d("any_failed", lambda s: s.any_failed, device_class=_PROBLEM),
    # Reported but rarely useful: present, off until switched on
    _d("cavity_overheated", lambda s: s.cavity_overheated, device_class=_PROBLEM, **_RARE),
    _d("triacs_failed", lambda s: s.triacs_failed, device_class=_PROBLEM, **_RARE),
    _d("lamp_failed", lambda s: s.lamp_failed, device_class=_PROBLEM, **_RARE),
    _d("top_element_failed", _failed("top"), device_class=_PROBLEM, **_RARE),
    _d("bottom_element_failed", _failed("bottom"), device_class=_PROBLEM, **_RARE),
    _d("rear_element_failed", _failed("rear"), device_class=_PROBLEM, **_RARE),
    _d("boiler_failed", _failed("boiler"), device_class=_PROBLEM, **_RARE),
    _d("evaporator_failed", _failed("evaporator"), device_class=_PROBLEM, **_RARE),
    _d("fan_failed", _failed("fan"), device_class=_PROBLEM, **_RARE),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: OvenRuntime = entry.runtime_data
    async_add_entities(OvenBinarySensor(runtime, entry.entry_id, d) for d in BINARY_SENSORS)


class OvenBinarySensor(JellyAnovaEntity, BinarySensorEntity):
    entity_description: JellyAnovaBinaryDescription

    @property
    def is_on(self) -> bool | None:
        state = self.oven_state
        return None if state is None else self.entity_description.value_fn(state)
