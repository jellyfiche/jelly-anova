"""Diagnostics: useful state, and nothing secret anywhere in the output."""

import json

from homeassistant.components.diagnostics import async_redact_data

from custom_components.jelly_anova.diagnostics import TO_REDACT, async_get_config_entry_diagnostics

from .test_runtime import report
from .test_setup import fake, setup  # noqa: F401

SECRETS = {
    "refresh_token": "SENTINEL-refresh",
    "api_key": "SENTINEL-apikey",  # leak-check: allow
    "personal_token": "SENTINEL-personal",
    "email": "SENTINEL-email",
    "password": "SENTINEL-password",  # leak-check: allow
    "uid": "SENTINEL-uid",
    "wifi_name": "SENTINEL-wifi",
    "processed_command_ids": ["SENTINEL-cmd1", "SENTINEL-cmd2"],
    "cookable_id": "SENTINEL-cookable",
    "unlisted_key": ["SENTINEL-id1", "SENTINEL-id2"],
}
COOKER = "SENTINEL-cooker"


def walk(obj, path=""):
    """Every key and value in a nested structure, as text."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{path}/{key}", str(key)
            yield from walk(value, f"{path}/{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from walk(value, f"{path}/{i}")
    else:
        yield path, str(obj)


async def test_diagnostics_redacts(hass, fake, hass_storage):  # noqa: F811
    entry = await setup(hass, {**SECRETS})
    runtime = entry.runtime_data
    runtime.connection.cooker_id = COOKER
    runtime.on_status(True)
    runtime.on_state(report("cook", "SENTINEL-cookid", stage_mode="running", dry=120))
    runtime._cooker_id = COOKER
    runtime._cook_id = "SENTINEL-cookid"
    await hass.async_block_till_done()
    out = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(out, default=str)
    for value in [*SECRETS.values(), COOKER, "SENTINEL-cookid"]:
        for part in value if isinstance(value, list) else [value]:
            assert part not in text, part
    assert out["entry"]["data_keys"] == sorted(SECRETS)
    assert "data" not in out["entry"] and "options" not in out["entry"]
    # Every path that holds a secret-named key is redacted, wherever it sits.
    for path, value in walk(out):
        assert "SENTINEL" not in value, path


async def test_diagnostics_has_useful_items(hass, fake):  # noqa: F811
    entry = await setup(hass, {"refresh_token": "SENTINEL-refresh", "api_key": "k"})
    runtime = entry.runtime_data
    runtime.on_status(True)
    runtime.on_state(report("idle"))
    out = await async_get_config_entry_diagnostics(hass, entry)
    oven = out["oven"]
    assert oven["connected"] is True
    assert oven["available"] is True
    assert oven["mode"] == "idle"
    assert oven["last_report"] is not None
    assert oven["cook_saved"] is False
    assert oven["saved_stop_at"] is None
    assert "total_cook_hours" in oven
    assert "firmware" in oven and "online" in oven


async def test_diagnostics_shows_saved_cook_without_its_id(hass, fake):  # noqa: F811
    entry = await setup(hass, {"refresh_token": "SENTINEL-refresh", "api_key": "k"})
    runtime = entry.runtime_data
    from datetime import UTC, datetime

    runtime._cook_id = "SENTINEL-cookid"
    runtime._stop_at = datetime(2030, 1, 1, tzinfo=UTC)
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["oven"]["cook_saved"] is True
    assert out["oven"]["saved_stop_at"].startswith("2030-01-01")
    assert "SENTINEL" not in json.dumps(out, default=str)


async def test_every_secret_key_is_redacted_at_any_depth():
    """A secret-named key is removed wherever it sits, so a new field cannot slip through."""
    nested = {key: f"SENTINEL-{key}" for key in TO_REDACT}
    tree = {"a": nested, "b": [{"c": dict(nested)}], **nested}
    for path, value in walk(async_redact_data(tree, TO_REDACT)):
        assert "SENTINEL" not in value, path
