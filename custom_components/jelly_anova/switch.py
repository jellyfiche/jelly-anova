"""Switches: the door light and the door light preference."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import JellyAnovaEntity
from .errors import plain_errors

DOOR_LIGHT = SwitchEntityDescription(key="door_light", translation_key="door_light")
DOOR_LIGHT_PREFERENCE = SwitchEntityDescription(
    key="door_light_preference", translation_key="door_light_preference", entity_category=EntityCategory.CONFIG
)
SHOW_COMMAND_SECONDS = 30  # the preference shows what was sent until a report agrees, up to this long


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        [
            DoorLight(runtime, entry.entry_id, DOOR_LIGHT),
            DoorLightPreference(runtime, entry.entry_id, DOOR_LIGHT_PREFERENCE),
        ]
    )


class DoorLight(JellyAnovaEntity, SwitchEntity):
    """CMD_APO_SET_LAMP. The oven's reports of the light lag or miss changes (proven 2026-11-01),
    so this shows the last command sent and says so (assumed state)."""

    _attr_assumed_state = True
    _commanded: bool | None = None

    @property
    def is_on(self) -> bool | None:
        return self._commanded

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(False)

    async def _send(self, on: bool) -> None:
        with plain_errors():
            await self.runtime.set_door_light(on)
        self._commanded = on
        self.async_write_ha_state()


class DoorLightPreference(JellyAnovaEntity, SwitchEntity):
    """CMD_APO_SET_LAMP_PREFERENCE: whether the light comes on by itself (reported as on or off)."""

    _sent: tuple[bool, float] | None = None

    @property
    def is_on(self) -> bool | None:
        state = self.oven_state
        reported = None if state is None or state.door_lamp_preference is None else state.door_lamp_preference == "on"
        if self._sent is not None:
            wanted, at = self._sent
            if reported != wanted and time.monotonic() - at < SHOW_COMMAND_SECONDS:
                return wanted
        return reported

    @callback
    def _changed(self) -> None:
        state = self.oven_state
        if self._sent is not None and state is not None and (state.door_lamp_preference == "on") == self._sent[0]:
            self._sent = None
        super()._changed()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(False)

    async def _send(self, on: bool) -> None:
        with plain_errors():
            await self.runtime.set_door_light_preference(on)
        self._sent = (on, time.monotonic())
        self.async_write_ha_state()
