"""The base for every Anova Precision Oven entity: its device, availability and live updates."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN
from .parts import PART_NAMES, part_of
from .runtime import OvenRuntime
from .state import OvenState

DEVICE_NAME = "Anova Precision Oven"
MANUFACTURER = "Anova"
MODEL = "Precision Oven 2.0"


def oven_device(entry_id: str) -> DeviceInfo:
    """The oven itself: the device the parts hang off, and the one that has the triggers."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)}, name=DEVICE_NAME, manufacturer=MANUFACTURER, model=MODEL
    )


def part_device(entry_id: str, part: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{part}")},
        name=f"{DEVICE_NAME} {PART_NAMES[part]}",
        manufacturer=MANUFACTURER,
        model=MODEL,
        via_device=(DOMAIN, entry_id),
    )


class JellyAnovaEntity(Entity):
    """Reads the runtime. Unavailable whenever the runtime is (no report for 60 s in a cook, 10 minutes idle)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: OvenRuntime, entry_id: str, description: EntityDescription) -> None:
        self.runtime = runtime
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        part = part_of(description.key)
        self._attr_device_info = oven_device(entry_id) if part is None else part_device(entry_id, part)

    @property
    def available(self) -> bool:
        return self.runtime.available

    @property
    def oven_state(self) -> OvenState | None:
        return self.runtime.state

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.add_listener(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()
