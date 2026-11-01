"""Numbers: the running cook's target temperature (°F) and the fan."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MAX_F
from .entity import JellyAnovaEntity
from .errors import plain_errors

TARGET = NumberEntityDescription(
    key="target_temperature",
    translation_key="target_temperature",
    device_class=NumberDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    native_min_value=50,
    native_max_value=MAX_F,
    native_step=1,
    mode=NumberMode.BOX,
)
FAN = NumberEntityDescription(
    key="fan",
    translation_key="fan",
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=0,
    native_max_value=100,
    native_step=1,
    mode=NumberMode.SLIDER,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    async_add_entities([TargetTemperature(runtime, entry.entry_id, TARGET), FanSpeed(runtime, entry.entry_id, FAN)])


class TargetTemperature(JellyAnovaEntity, NumberEntity):
    """Changes the running cook's temperature. Unavailable when no cook is running."""

    def _running_stage(self) -> bool:
        state = self.oven_state
        cook = state.cook if state else None
        return state is not None and state.mode not in (None, "idle") and cook is not None and not cook.in_end_stage

    @property
    def available(self) -> bool:
        return super().available and self._running_stage()

    @property
    def native_value(self) -> float | None:
        state = self.oven_state
        if state is None or state.target_c is None or not self._running_stage():
            return None
        return round(state.target_c * 9 / 5 + 32, 1)

    async def async_set_native_value(self, value: float) -> None:
        with plain_errors():
            await self.runtime.update({"temperature_f": value})


class FanSpeed(JellyAnovaEntity, NumberEntity):
    """The fan, 0 to 100. Works while idle. The oven's reports give the fan as words (low, mid,
    high), so this shows the last value sent."""

    _attr_assumed_state = True
    _commanded: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._commanded

    async def async_set_native_value(self, value: float) -> None:
        with plain_errors():
            await self.runtime.set_fan(value)
        self._commanded = value
        self.async_write_ha_state()
