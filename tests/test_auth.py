"""Tests for the Anova login and token renewal (HTTP is mocked)."""

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.jelly_anova import auth
from custom_components.jelly_anova.auth import (
    AnovaAuth,
    AuthFailed,
    CannotConnect,
    find_api_key,
    sign_in,
)

KEY = "fake-api-key"
SIGN_IN_URL = f"{auth.SIGN_IN_URL}?key={KEY}"
RENEW_URL = f"{auth.RENEW_URL}?key={KEY}"


def fake_real_shaped_key():
    """Built at run time so no key-shaped text sits in the source."""
    return "AIza" + "FAKE" * 8 + "xyz"


async def test_sign_in_ok(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, json={"refreshToken": "refresh-1", "localId": "u"})
    session = async_get_clientsession(hass)
    assert await sign_in(session, KEY, "a@example.com", "pw") == "refresh-1"
    body = aioclient_mock.mock_calls[0][2]
    assert body == {"email": "a@example.com", "password": "pw", "returnSecureToken": True}


async def test_sign_in_refused_plain_error(hass, aioclient_mock):
    aioclient_mock.post(
        SIGN_IN_URL, status=400, json={"error": {"message": "INVALID_PASSWORD"}}
    )
    session = async_get_clientsession(hass)
    with pytest.raises(AuthFailed) as err:
        await sign_in(session, KEY, "a@example.com", "pw")
    text = str(err.value)
    for secret in ("a@example.com", "pw", KEY, "googleapis", "INVALID_PASSWORD"):
        assert secret not in text


async def test_sign_in_unreachable(hass, aioclient_mock):
    import aiohttp

    aioclient_mock.post(SIGN_IN_URL, exc=aiohttp.ClientError("https://secret-url token"))
    with pytest.raises(CannotConnect) as err:
        await sign_in(async_get_clientsession(hass), KEY, "a@example.com", "pw")
    assert "secret-url" not in str(err.value)


def renew_ok(token="id-1", expires="3600", refresh=None):
    data = {"id_token": token, "expires_in": expires}
    if refresh:
        data["refresh_token"] = refresh
    return data


async def test_token_cached(hass, aioclient_mock, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    aioclient_mock.post(RENEW_URL, json=renew_ok())
    a = AnovaAuth(async_get_clientsession(hass), KEY, "refresh-1")
    assert await a.access_token() == "id-1"
    now[0] += 3000  # well inside the hour, and more than 5 minutes left
    assert await a.access_token() == "id-1"
    assert aioclient_mock.call_count == 1
    form = aioclient_mock.mock_calls[0][2]
    assert form == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


async def test_token_renewed_near_expiry(hass, aioclient_mock, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    aioclient_mock.post(RENEW_URL, json=renew_ok("id-1"))
    a = AnovaAuth(async_get_clientsession(hass), KEY, "refresh-1")
    assert await a.access_token() == "id-1"
    now[0] += 3600 - 299  # less than 5 minutes left
    aioclient_mock.clear_requests()
    aioclient_mock.post(RENEW_URL, json=renew_ok("id-2", refresh="refresh-2"))
    assert await a.access_token() == "id-2"
    assert a.refresh_token == "refresh-2"


async def test_renew_refused_raises(hass, aioclient_mock):
    aioclient_mock.post(RENEW_URL, status=400, json={"error": {"message": "TOKEN_EXPIRED"}})
    a = AnovaAuth(async_get_clientsession(hass), KEY, "refresh-1")
    with pytest.raises(AuthFailed):
        await a.access_token()


async def test_renew_server_trouble_is_not_auth_failure(hass, aioclient_mock):
    aioclient_mock.post(RENEW_URL, status=503)
    a = AnovaAuth(async_get_clientsession(hass), KEY, "refresh-1")
    with pytest.raises(CannotConnect):
        await a.access_token()


async def test_find_api_key(hass, aioclient_mock):
    key = fake_real_shaped_key()
    aioclient_mock.get(
        "https://oven.anovaculinary.com",
        text='<script src="/a.js"></script><script src="https://other.example/x.js"></script>'
        '<script src="//cdn.example/y.js"></script><script src="/b.js"></script>',
    )
    aioclient_mock.get("https://oven.anovaculinary.com/a.js", text="var x=1;")
    aioclient_mock.get("https://oven.anovaculinary.com/b.js", text=f'k:"{key}"')
    assert await find_api_key(async_get_clientsession(hass)) == key
    assert aioclient_mock.call_count == 3  # page, a.js, b.js: nothing off-site


async def test_find_api_key_none(hass, aioclient_mock):
    aioclient_mock.get("https://oven.anovaculinary.com", status=500)
    assert await find_api_key(async_get_clientsession(hass)) is None


async def test_bad_key_is_not_a_password_problem(hass, aioclient_mock):
    aioclient_mock.post(
        SIGN_IN_URL,
        status=400,
        json={"error": {"message": "API key not valid. Please pass a valid API key."}},
    )
    with pytest.raises(auth.InvalidApiKey) as err:
        await sign_in(async_get_clientsession(hass), KEY, "a@example.com", "pw")
    assert KEY not in str(err.value) and "API key not valid" not in str(err.value)


async def test_blocked_key_403_is_a_key_problem(hass, aioclient_mock):
    aioclient_mock.post(RENEW_URL, status=403, json={"error": {"message": "PERMISSION_DENIED"}})
    a = AnovaAuth(async_get_clientsession(hass), KEY, "refresh-1")
    with pytest.raises(auth.InvalidApiKey):
        await a.access_token()


@pytest.mark.parametrize(
    "reason",
    [
        "INVALID_PASSWORD",
        "INVALID_LOGIN_CREDENTIALS",
        "EMAIL_NOT_FOUND",
        "INVALID_REFRESH_TOKEN",
        "TOKEN_EXPIRED",
        "USER_DISABLED",
        "USER_NOT_FOUND",
    ],
)
async def test_password_and_token_reasons_are_auth_failed(hass, aioclient_mock, reason):
    aioclient_mock.post(SIGN_IN_URL, status=400, json={"error": {"message": f"{reason} : detail"}})
    with pytest.raises(AuthFailed):
        await sign_in(async_get_clientsession(hass), KEY, "a@example.com", "pw")


async def test_unknown_400_is_not_auth_failed(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, status=400, text="not json")
    with pytest.raises(CannotConnect):
        await sign_in(async_get_clientsession(hass), KEY, "a@example.com", "pw")


async def test_find_api_key_caps_scripts(hass, aioclient_mock):
    tags = "".join(f'<script src="/s{i}.js"></script>' for i in range(60))
    aioclient_mock.get("https://oven.anovaculinary.com", text=tags)
    for i in range(60):
        aioclient_mock.get(f"https://oven.anovaculinary.com/s{i}.js", text="x")
    assert await find_api_key(async_get_clientsession(hass)) is None
    assert aioclient_mock.call_count == 1 + 40


async def test_find_api_key_deadline(monkeypatch):
    import asyncio

    class Slow:
        def get(self, *a, **k):
            return self

        async def __aenter__(self):
            await asyncio.sleep(5)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(auth, "SEARCH_DEADLINE", 0.05)
    assert await find_api_key(Slow()) is None
