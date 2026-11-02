"""Anova Precision Oven: unofficial Home Assistant integration for the Anova Precision Oven."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .auth import AnovaAuth
from .connection import OvenConnection
from .const import DOMAIN
from .runtime import OvenRuntime
from .services import async_setup_services
from .state import OvenState

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SELECT,
]
UNLOAD_WAIT = 10  # seconds unload waits for the connection to finish
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions, so they exist with no oven set up and then say so."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Build the login, connection and runtime, and start connecting."""
    session = async_get_clientsession(hass)
    auth = AnovaAuth(session, entry.data["api_key"], entry.data["refresh_token"])

    # The connection needs these callbacks and the runtime needs the connection.
    @callback
    def on_state(state: OvenState) -> None:
        runtime.on_state(state)

    @callback
    def on_status(up: bool) -> None:
        # Tokens are renewed on connecting, so this is when a changed one shows.
        if auth.refresh_token != entry.data.get("refresh_token"):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "refresh_token": auth.refresh_token}
            )
        runtime.on_status(up)

    @callback
    def on_auth_failed() -> None:
        runtime.on_auth_failed()

    connection = OvenConnection(session, auth, on_state, on_status, on_auth_failed)
    runtime = OvenRuntime(hass, entry, connection, session)
    await runtime.restore()
    runtime.run_task = entry.async_create_background_task(
        hass, connection.run(), "jelly_anova connection"
    )
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Close the connection and wait for it. Cancelling run() alone leaves the reader alive."""
    runtime: OvenRuntime = entry.runtime_data
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await runtime.connection.close()
    if runtime.run_task is not None:
        _, pending = await asyncio.wait([runtime.run_task], timeout=UNLOAD_WAIT)
        for task in pending:
            task.cancel()
    await runtime.shutdown()
    return True
