"""Tests for the setup screen (HTTP is mocked)."""

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jelly_anova import auth
from custom_components.jelly_anova.const import DOMAIN

KEY = "fake-api-key"
SIGN_IN_URL = f"{auth.SIGN_IN_URL}?key={KEY}"
RENEW_URL = f"{auth.RENEW_URL}?key={KEY}"
EMAIL = "someone@example.com"
OLD_KEY = "stale-value"
TYPED = "placeholder-12345"


@pytest.fixture
def key_found(monkeypatch):
    async def found(session):
        return KEY

    monkeypatch.setattr("custom_components.jelly_anova.config_flow.find_api_key", found)


@pytest.fixture
def key_missing(monkeypatch):
    async def missing(session):
        return None

    monkeypatch.setattr("custom_components.jelly_anova.config_flow.find_api_key", missing)


async def start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return result


async def test_flow_never_stores_password(hass, aioclient_mock, key_found):
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-1", "localId": "uid"})
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["step_id"] == "personal"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"personal_token": "pat-1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data == {"refresh_token": "refresh-1", "personal_token": "pat-1", "api_key": KEY}
    assert EMAIL not in repr(result) and TYPED not in repr(result)
    assert EMAIL not in repr(result["result"].unique_id)


async def test_flow_wrong_password_plain_error(hass, aioclient_mock, key_found):
    aioclient_mock.post(SIGN_IN_URL, status=400, json={"error": {"message": "INVALID_PASSWORD"}})
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_flow_cannot_connect(hass, aioclient_mock, key_found):
    aioclient_mock.post(SIGN_IN_URL, status=503)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_flow_asks_for_key_when_not_found(hass, aioclient_mock, key_missing):
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-1"})
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    assert "api_key" in result["data_schema"].schema
    # Leaving it empty is refused in plain words.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["errors"] == {"base": "no_api_key"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED, "api_key": KEY}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["api_key"] == KEY
    assert result["data"]["personal_token"] == ""


async def test_flow_paste_path(hass, aioclient_mock, key_found):
    aioclient_mock.post(RENEW_URL, json={"id_token": "id-1", "expires_in": "3600"})
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "paste"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "pasted-refresh"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["refresh_token"] == "pasted-refresh"


async def test_flow_paste_bad_token(hass, aioclient_mock, key_found):
    aioclient_mock.post(RENEW_URL, status=400, json={"error": {"message": "INVALID_REFRESH_TOKEN"}})
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "paste"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "nope"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_only_one_entry(hass, aioclient_mock, key_found):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth_updates_token(hass, aioclient_mock, key_found):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"refresh_token": "old", "personal_token": "pat-1", "api_key": KEY},
    )
    entry.add_to_hass(hass)
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-new"})
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {"refresh_token": "refresh-new", "personal_token": "pat-1", "api_key": KEY}


BAD_KEY_BODY = {"error": {"message": "API key not valid. Please pass a valid API key."}}


def discovers(monkeypatch, value):
    async def found(session):
        return value

    monkeypatch.setattr("custom_components.jelly_anova.config_flow.find_api_key", found)


async def test_reauth_stale_stored_key_is_replaced_by_fresh_discovery(
    hass, aioclient_mock, monkeypatch
):
    stale_url = f"{auth.SIGN_IN_URL}?key={OLD_KEY}"
    aioclient_mock.post(stale_url, status=400, json=BAD_KEY_BODY)
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-new"})
    discovers(monkeypatch, KEY)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"refresh_token": "old", "personal_token": "", "api_key": OLD_KEY},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == KEY
    assert entry.data["refresh_token"] == "refresh-new"


async def test_reauth_falls_back_to_stored_key_then_asks(hass, aioclient_mock, monkeypatch):
    stale_url = f"{auth.SIGN_IN_URL}?key={OLD_KEY}"
    aioclient_mock.post(stale_url, status=400, json=BAD_KEY_BODY)
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-new"})
    discovers(monkeypatch, None)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"refresh_token": "old", "personal_token": "", "api_key": OLD_KEY},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["errors"] == {"base": "invalid_api_key"}
    assert "api_key" in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED, "api_key": KEY}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == KEY


async def test_typed_key_overrides_found_key(hass, aioclient_mock, monkeypatch):
    bad_url = f"{auth.SIGN_IN_URL}?key=found-bad"
    aioclient_mock.post(bad_url, status=400, json=BAD_KEY_BODY)
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-1"})
    discovers(monkeypatch, "found-bad")
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    assert "api_key" not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED}
    )
    assert result["errors"] == {"base": "invalid_api_key"}
    assert "api_key" in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": TYPED, "api_key": KEY}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["api_key"] == KEY


async def test_failed_discovery_is_not_repeated(hass, aioclient_mock, monkeypatch):
    calls = []

    async def missing(session):
        calls.append(1)

    monkeypatch.setattr("custom_components.jelly_anova.config_flow.find_api_key", missing)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    for _ in range(2):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": EMAIL, "password": TYPED}
        )
    assert len(calls) == 1
