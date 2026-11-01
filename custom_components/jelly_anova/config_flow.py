"""Setup screen for Jelly Anova.

Asks for the Anova email and password once, swaps them for a refresh token and keeps only that.
Only one Anova account is set up at a time (single instance), so no email or account id is
stored or needed as a unique id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import AnovaAuth, AuthFailed, CannotConnect, InvalidApiKey, find_api_key, sign_in
from .const import DOMAIN


class JellyAnovaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in, or paste a refresh token, then optionally add the personal token."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._refresh_token: str | None = None
        self._stored_key: str | None = None
        self._tried_discovery = False
        self._ask_key = False
        self._reauth_entry = None

    async def _discover(self) -> None:
        """Read the public web key from Anova's site once per flow, even if it fails.

        A key found now beats a stored one; with none found the stored key is kept.
        """
        if self._tried_discovery:
            return
        self._tried_discovery = True
        found = await find_api_key(async_get_clientsession(self.hass))
        if found:
            self._api_key = found
        else:
            self._api_key = self._stored_key
            self._ask_key = True

    def _key_trouble(self, errors: dict) -> None:
        """The key could be the problem: show the key box."""
        if errors.get("base") in ("invalid_api_key", "cannot_connect"):
            self._ask_key = True

    def _schema(self, fields: dict, ask_key: bool) -> vol.Schema:
        if ask_key:  # a typed key replaces a found or stored one
            fields = {**fields, vol.Optional("api_key", default=""): str}
        return vol.Schema(fields)

    def _take_key(self, user_input: dict) -> None:
        typed = (user_input.get("api_key") or "").strip()
        if typed:
            self._api_key = typed

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_show_menu(step_id="user", menu_options=["login", "paste"])

    async def async_step_login(self, user_input=None) -> ConfigFlowResult:
        return await self._login("login", user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self._stored_key = entry_data.get("api_key") or None
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        return await self._login("reauth_confirm", user_input)

    async def _login(self, step_id: str, user_input) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        await self._discover()
        if user_input is not None:
            self._take_key(user_input)
            if not self._api_key:
                errors["base"] = "no_api_key"
            else:
                try:
                    self._refresh_token = await sign_in(
                        async_get_clientsession(self.hass),
                        self._api_key,
                        user_input["email"].strip(),
                        user_input["password"],
                    )
                except AuthFailed:
                    errors["base"] = "invalid_auth"
                except InvalidApiKey:
                    errors["base"] = "invalid_api_key"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    if self._reauth_entry is not None:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data_updates={
                                "refresh_token": self._refresh_token,
                                "api_key": self._api_key,
                            },
                            reason="reauth_successful",
                        )
                    return await self.async_step_personal()
        self._key_trouble(errors)
        schema = self._schema(
            {vol.Required("email"): str, vol.Required("password"): str}, self._ask_key
        )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_paste(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        await self._discover()
        if user_input is not None:
            self._take_key(user_input)
            token = user_input["refresh_token"].strip()
            if not self._api_key:
                errors["base"] = "no_api_key"
            else:
                check = AnovaAuth(async_get_clientsession(self.hass), self._api_key, token)
                try:
                    await check.access_token()
                except AuthFailed:
                    errors["base"] = "invalid_auth"
                except InvalidApiKey:
                    errors["base"] = "invalid_api_key"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    self._refresh_token = check.refresh_token
                    return await self.async_step_personal()
        self._key_trouble(errors)
        schema = self._schema({vol.Required("refresh_token"): str}, self._ask_key)
        return self.async_show_form(step_id="paste", data_schema=schema, errors=errors)

    async def async_step_personal(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="Anova Precision Oven",
                data={
                    "refresh_token": self._refresh_token,
                    "personal_token": (user_input.get("personal_token") or "").strip(),
                    "api_key": self._api_key,
                },
            )
        schema = vol.Schema({vol.Optional("personal_token", default=""): str})
        return self.async_show_form(step_id="personal", data_schema=schema)
