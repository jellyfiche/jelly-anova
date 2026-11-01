"""The base for every Jelly Anova entity: one device, availability and live updates."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN
from .runtime import OvenRuntime
from .state import OvenState

DEVICE_NAME = "Anova Precision Oven"


class JellyAnovaEntity(Entity):
    """Reads the runtime. Unavailable whenever the runtime is (no fresh report for 60 s)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: OvenRuntime, entry_id: str, description: EntityDescription) -> None:
        self.runtime = runtime
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=DEVICE_NAME,
            manufacturer="Anova",
            model="Precision Oven 2.0",
        )

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
