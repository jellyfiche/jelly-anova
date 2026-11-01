"""Smoke test: the integration can be found and set up."""

from homeassistant.setup import async_setup_component

from custom_components.jelly_anova.const import DOMAIN


async def test_integration_loads(hass):
    """With no config entries, setting up the domain succeeds."""
    assert await async_setup_component(hass, DOMAIN, {})


async def test_entry_unload(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
