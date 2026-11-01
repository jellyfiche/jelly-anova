"""The oven's display unit."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import JellyAnovaEntity
from .errors import plain_errors

DISPLAY_UNIT = SelectEntityDescription(
    key="display_unit",
    translation_key="display_unit",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=False,  # unproven: enable it once Task 11 has tried the command
    options=["celsius", "fahrenheit"],
)
UNITS = {"celsius": "C", "fahrenheit": "F"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DisplayUnit(entry.runtime_data, entry.entry_id, DISPLAY_UNIT)])


class DisplayUnit(JellyAnovaEntity, SelectEntity):
    """CMD_APO_SET_TEMPERATURE_UNIT. UNPROVEN: it is in Anova's docs but not yet tried on this
    oven (Task 11). The reports' unit is not read, so this shows the last choice made here."""

    _attr_assumed_state = True
    _chosen: str | None = None

    @property
    def current_option(self) -> str | None:
        return self._chosen

    async def async_select_option(self, option: str) -> None:
        with plain_errors():
            await self.runtime.set_display_unit(UNITS[option])
        self._chosen = option
        self.async_write_ha_state()
