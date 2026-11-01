"""Device triggers: the two oven events, filtered to the one device."""

import pytest
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType, async_get_device_automations
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.jelly_anova.const import DOMAIN

from .test_runtime import report
from .test_setup import DATA, fake, setup  # noqa: F401


async def oven(hass):
    entry = await setup(hass)
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    assert device is not None
    return entry, device


async def test_triggers_listed(hass, fake):
    _, device = await oven(hass)
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, [device.id])
    listed = {t["type"] for t in triggers[device.id] if t["domain"] == DOMAIN}
    assert listed == {"cook_target_reached", "cook_finished"}


async def test_trigger_fires(hass, fake):
    entry, device = await oven(hass)
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device.id,
                        "type": kind,
                    },
                    "action": {"service": "test.automation", "data": {"kind": kind}},
                }
                for kind in ("cook_target_reached", "cook_finished")
            ]
        },
    )
    runtime = entry.runtime_data
    runtime.on_state(report("cook", "c1", stage_mode="entering", dry=40))
    runtime.on_state(report("cook", "c1", stage_mode="running", dry=120))
    await hass.async_block_till_done()
    assert [c.data["kind"] for c in calls] == ["cook_target_reached"]
    runtime.on_state(report("idle"))
    await hass.async_block_till_done()
    assert [c.data["kind"] for c in calls] == ["cook_target_reached", "cook_finished"]


async def test_trigger_ignores_another_device(hass, fake):
    entry, _ = await oven(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("other", "x")}
    )
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "trigger": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": other.id,
                    "type": "cook_finished",
                },
                "action": {"service": "test.automation"},
            }
        },
    )
    runtime = entry.runtime_data
    runtime.on_state(report("cook", "c1", stage_mode="running", dry=120))
    runtime.on_state(report("idle"))
    await hass.async_block_till_done()
    assert calls == []
