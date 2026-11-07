"""Tests for the oven camera entity: live video during a cook, through Home Assistant's WebRTC."""

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

from custom_components.jelly_anova.stream import CameraFailed, OvenLiveStream

from .test_sensor import fresh, load, push
from .test_setup import fake, setup  # noqa: F401 - fake is a fixture

CAMERA = "camera.anova_precision_oven_camera"


@pytest.fixture
def viewers(monkeypatch):
    """Stands in for the oven's stream: records viewers, answers offers."""
    seen = {"watch": [], "leave": [], "fail": None}

    async def watch(self, viewer, offer_sdp):
        seen["watch"].append((viewer, offer_sdp))
        if seen["fail"]:
            raise seen["fail"]
        return "v=0 answer"

    async def leave(self, viewer):
        seen["leave"].append(viewer)

    monkeypatch.setattr(OvenLiveStream, "watch", watch)
    monkeypatch.setattr(OvenLiveStream, "leave", leave)
    return seen


async def cooking(hass, fake):  # noqa: F811
    entry = await setup(hass)
    await push(hass, entry, fresh(load("login_cook.json")))
    return entry


async def offer(hass, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "camera/webrtc/offer", "entity_id": CAMERA, "offer": "v=0 offer"})
    reply = await client.receive_json()
    assert reply["success"], reply
    subscription = reply["id"]
    session = await client.receive_json()
    assert session["event"]["type"] == "session"
    event = await client.receive_json()
    return client, subscription, session["event"]["session_id"], event["event"]


async def test_entity_id_and_name(hass, fake):  # noqa: F811
    await cooking(hass, fake)
    state = hass.states.get(CAMERA)
    assert state is not None
    assert state.attributes["friendly_name"] == "Anova Precision Oven Camera"
    assert er.async_get(hass).async_get(CAMERA).unique_id.endswith("_camera")


async def test_available_during_cook(hass, fake):  # noqa: F811
    await cooking(hass, fake)
    assert hass.states.get(CAMERA).state != STATE_UNAVAILABLE


async def test_unavailable_when_idle(hass, fake):  # noqa: F811
    entry = await setup(hass)
    await push(hass, entry, fresh(load("idle.json")))
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE


async def test_no_picture_attribute(hass, fake):  # noqa: F811
    await cooking(hass, fake)
    assert "entity_picture" not in hass.states.get(CAMERA).attributes


async def test_offer_returns_answer(hass, fake, viewers, hass_ws_client):  # noqa: F811
    await cooking(hass, fake)
    _, _, session_id, event = await offer(hass, hass_ws_client)
    assert event == {"type": "answer", "answer": "v=0 answer"}
    assert viewers["watch"] == [(session_id, "v=0 offer")]


async def test_offer_failure_plain_error(hass, fake, viewers, hass_ws_client):  # noqa: F811
    await cooking(hass, fake)
    viewers["fail"] = CameraFailed("The oven's camera did not start")
    _, _, _, event = await offer(hass, hass_ws_client)
    assert event["type"] == "error"
    assert event["message"] == "The oven's camera did not start"


async def test_close_session_leaves(hass, fake, viewers, hass_ws_client):  # noqa: F811
    await cooking(hass, fake)
    client, subscription, session_id, _ = await offer(hass, hass_ws_client)
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": subscription})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert viewers["leave"] == [session_id]


async def test_stun_server_given_to_the_viewer(hass, fake, hass_ws_client):  # noqa: F811
    await cooking(hass, fake)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "camera/webrtc/get_client_config", "entity_id": CAMERA})
    reply = await client.receive_json()
    assert reply["success"], reply
    urls = [server["urls"] for server in reply["result"]["configuration"]["iceServers"]]
    assert "stun:stun.cloudflare.com:3478" in str(urls)
