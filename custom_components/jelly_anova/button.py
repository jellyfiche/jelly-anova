"""The Stop button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import JellyAnovaEntity
from .errors import plain_errors
from .runtime import OvenRuntime

STOP = ButtonEntityDescription(key="stop", translation_key="stop")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([StopButton(entry.runtime_data, entry.entry_id, STOP)])


class StopButton(JellyAnovaEntity, ButtonEntity):
    """Always sends the stop, whatever the last report says."""

    @property
    def available(self) -> bool:
        # Stop stays pressable when reports are stale: not knowing is the worst time to hide it.
        # If the oven cannot be reached, pressing it says so in plain words.
        return True

    async def async_press(self) -> None:
        runtime: OvenRuntime = self.runtime
        with plain_errors():
            await runtime.stop()
