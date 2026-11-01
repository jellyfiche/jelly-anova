"""Anova login: email and password to a refresh token, and token renewal.

Nothing here logs or puts in an error message a URL, token, email, password or the text of a
connection error.
"""

from __future__ import annotations

import asyncio
import re
import time

import aiohttp

SITE = "https://oven.anovaculinary.com"
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
RENEW_URL = "https://securetoken.googleapis.com/v1/token"

RENEW_MARGIN = 300  # renew when less than 5 minutes are left
DEFAULT_LIFETIME = 3600
MAX_SCRIPTS = 40  # most scripts read when looking for the web key
SEARCH_DEADLINE = 60  # seconds for the whole key search
# Google's reasons that mean the person's password or login key is wrong.
AUTH_REASONS = {
    "INVALID_PASSWORD",
    "INVALID_LOGIN_CREDENTIALS",
    "EMAIL_NOT_FOUND",
    "INVALID_REFRESH_TOKEN",
    "TOKEN_EXPIRED",
    "USER_DISABLED",
    "USER_NOT_FOUND",
}
TIMEOUT = aiohttp.ClientTimeout(total=20)

_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["'](/[^"']*\.js)["']""", re.IGNORECASE)
_API_KEY = re.compile(r"AIza[0-9A-Za-z_-]{35}")


class AuthFailed(Exception):
    """Anova refused the email and password, or the stored login key."""


class InvalidApiKey(Exception):
    """Anova's web key was refused (stale, wrong or blocked), not the password."""


class CannotConnect(Exception):
    """Anova or its sign-in service could not be reached or had a problem."""


async def _refusal(reply) -> Exception:
    """Decide from Google's reason what a 4xx means. The reason text is never repeated."""
    try:
        body = await reply.json(content_type=None)
        message = str(body["error"]["message"])
    except (aiohttp.ClientError, ValueError, KeyError, TypeError):
        message = ""
    if message.split(" ")[0].strip(":") in AUTH_REASONS:
        return AuthFailed("Anova refused the login")
    if reply.status == 403 or "API key" in message or "API_KEY" in message:
        return InvalidApiKey("Anova refused the web key")
    return CannotConnect("Anova could not be reached")


async def _post(session, url, key, **kwargs) -> dict:
    """POST and return the JSON reply; map failures to the two plain exceptions."""
    try:
        async with session.post(
            url, params={"key": key}, timeout=TIMEOUT, **kwargs
        ) as reply:
            if reply.status in (400, 401, 403):
                raise await _refusal(reply)
            if reply.status != 200:
                raise CannotConnect("Anova could not be reached")
            return await reply.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        # The original error can carry the URL, so it is dropped (from None).
        raise CannotConnect("Anova could not be reached") from None


async def sign_in(session, api_key: str, email: str, password: str) -> str:
    """Exchange email and password for a refresh token. The password is not kept."""
    data = await _post(
        session,
        SIGN_IN_URL,
        api_key,
        json={"email": email, "password": password, "returnSecureToken": True},
    )
    token = data.get("refreshToken") if isinstance(data, dict) else None
    if not token:
        raise CannotConnect("Anova gave no login key")
    return token


async def find_api_key(session) -> str | None:
    """Read Anova's public web API key from its own website, or None if not found."""
    try:
        async with asyncio.timeout(SEARCH_DEADLINE):
            async with session.get(SITE, timeout=TIMEOUT) as page:
                if page.status != 200:
                    return None
                html = await page.text()
            paths = [p for p in dict.fromkeys(_SCRIPT_SRC.findall(html)) if not p.startswith("//")]
            for path in paths[:MAX_SCRIPTS]:
                async with session.get(SITE + path, timeout=TIMEOUT) as script:
                    if script.status != 200:
                        continue
                    found = _API_KEY.search(await script.text())
                if found:
                    return found.group(0)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    return None


class AnovaAuth:
    """Holds the refresh token and hands out a fresh access token when asked."""

    def __init__(self, session, api_key: str, refresh_token: str) -> None:
        self._session = session
        self._api_key = api_key
        self.refresh_token = refresh_token
        self._access_token: str | None = None
        self._renew_at = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        """Return the access token, renewing it when under 5 minutes remain."""
        async with self._lock:
            if self._access_token and time.monotonic() < self._renew_at:
                return self._access_token
            data = await _post(
                self._session,
                RENEW_URL,
                self._api_key,
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            )
            token = data.get("id_token") if isinstance(data, dict) else None
            if not token:
                raise CannotConnect("Anova gave no access token")
            try:
                lifetime = int(data.get("expires_in", DEFAULT_LIFETIME))
            except (TypeError, ValueError):
                lifetime = DEFAULT_LIFETIME
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
            self._access_token = token
            self._renew_at = time.monotonic() + lifetime - RENEW_MARGIN
            return token
