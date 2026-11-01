"""Jelly Anova: unofficial Home Assistant integration for the Anova Precision Oven."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Accept the entry so setup can finish; connecting is added in Task 7."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nothing is running yet; the connection part is added in Task 7."""
    return True
