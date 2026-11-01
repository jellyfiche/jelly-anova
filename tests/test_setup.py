"""Smoke test: the integration can be found and set up."""

from homeassistant.setup import async_setup_component

from custom_components.jelly_anova.const import DOMAIN


async def test_integration_loads(hass):
    """With no config entries, setting up the domain succeeds."""
    assert await async_setup_component(hass, DOMAIN, {})
