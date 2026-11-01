"""Setting up and unloading the integration. A fake connection stands in for the websocket."""

import asyncio

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.jelly_anova as integration
from custom_components.jelly_anova.const import DOMAIN
from custom_components.jelly_anova.runtime import OvenRuntime

from .test_connection import FUTURE, FakeAuth, FakeSession, FakeWS, report, until

DATA = {"refresh_token": "old-key", "api_key": "web-key", "personal_token": ""}


class FakeConnection:
    def __init__(self, session, auth, on_state, on_status, on_auth_failed=None):
        self.auth, self.on_state, self.on_status = auth, on_state, on_status
        self.on_auth_failed = on_auth_failed
        self.cooker_id = None
        self.closed = False
        self.finished = False
        self._release = asyncio.Event()
        FakeConnection.made.append(self)

    async def run(self):
        await self._release.wait()
        self.finished = True

    async def close(self):
        self.closed = True
        self._release.set()


@pytest.fixture
def fake(monkeypatch):
    FakeConnection.made = []
    monkeypatch.setattr(integration, "OvenConnection", FakeConnection)

    async def no_key(session):
        return "web-key"

    monkeypatch.setattr("custom_components.jelly_anova.config_flow.find_api_key", no_key)
    return FakeConnection


async def setup(hass, data=DATA):
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry


async def test_integration_loads(hass):
    """With no config entries, setting up the domain succeeds."""
    assert await async_setup_component(hass, DOMAIN, {})


async def test_entry_unload(hass, fake):
    entry = await setup(hass)
    assert isinstance(entry.runtime_data, OvenRuntime)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_starts_run_and_unload_closes_and_waits(hass, fake):
    entry = await setup(hass)
    connection = fake.made[0]
    await asyncio.sleep(0)
    assert connection.finished is False
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert connection.closed is True
    assert connection.finished is True


async def test_callbacks_reach_the_runtime(hass, fake):
    entry = await setup(hass)
    fake.made[0].on_status(True)
    assert entry.runtime_data._connected is True


async def test_login_refused_starts_reauth(hass, fake):
    await setup(hass)
    fake.made[0].on_auth_failed()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_changed_refresh_token_saved_to_entry(hass, fake):
    entry = await setup(hass)
    connection = fake.made[0]
    connection.on_status(True)
    assert entry.data["refresh_token"] == "old-key"
    connection.auth.refresh_token = "new-key"
    connection.on_status(True)
    assert entry.data["refresh_token"] == "new-key"
    assert entry.data["api_key"] == "web-key"


async def test_restored_limit_is_live_after_setup(hass, hass_storage, fake):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"cook_id": "ios-a", "stop_at": "2999-01-01T00:00:00+00:00", "total_cook_hours": 3.0},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.total_cook_hours == 3.0


async def test_unload_gives_up_on_a_run_that_will_not_end(hass, fake, monkeypatch):
    monkeypatch.setattr(integration, "UNLOAD_WAIT", 0.01)

    class Stuck(FakeConnection):
        async def close(self):
            self.closed = True  # run() is left waiting

    monkeypatch.setattr(integration, "OvenConnection", Stuck)
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert fake.made[0].closed is True


async def test_real_connection_end_to_end(hass, monkeypatch):
    """The real connection and runtime together: a report arrives, Stop reaches the socket."""
    ws = FakeWS(reply=lambda sent: {"command": "RESPONSE", "requestId": sent["requestId"], "payload": {"status": "ok"}})
    auth = FakeAuth()
    auth.refresh_token = DATA["refresh_token"]
    monkeypatch.setattr(integration, "async_get_clientsession", lambda hass: FakeSession(ws))
    monkeypatch.setattr(integration, "AnovaAuth", lambda *args: auth)
    entry = await setup(hass)
    runtime = entry.runtime_data
    ws.push(report(FUTURE))
    await until(lambda: runtime.available)
    assert runtime.state.mode == "idle"
    await runtime.stop()
    assert [sent["command"] for sent in ws.sent] == ["CMD_APO_STOP"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ws.closed is True
    assert runtime.run_task.done()
