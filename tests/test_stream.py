"""Tests for the oven camera's live stream: one oven stream shared by every viewer."""

import asyncio
import logging
from datetime import timedelta

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.jelly_anova import stream as stream_module
from custom_components.jelly_anova.connection import OvenOffline, OvenRefused
from custom_components.jelly_anova.stream import CameraFailed, OvenLiveStream

START = "CMD_APO_START_LIVE_STREAM"
STOP = "CMD_APO_STOP_LIVE_STREAM"
LINK = "https://whep.example/live/abc/webRTC/play"
RESOURCE = "https://whep.example/live/abc/webRTC/play/session-1"
LINKED = {"status": "ok", "data": {"webRTCPlayback": {"url": LINK}}}


class FakeConnection:
    """Records commands. `replies` are returned (or raised) by the next sends, in order."""

    def __init__(self):
        self.sent = []
        self.replies = []

    async def send(self, command, payload, timeout=10):
        self.sent.append((command, payload))
        reply = self.replies.pop(0) if self.replies else (LINKED if command == START else {"status": "ok"})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def count(self, command):
        return sum(1 for sent, _ in self.sent if sent == command)


@pytest.fixture
def quick(monkeypatch):
    """No real waiting where timing is not what the test is about."""
    monkeypatch.setattr(stream_module, "FIRST_OFFER_WAIT", 0)
    monkeypatch.setattr(stream_module, "OFFER_RETRY_DELAYS", (0, 0, 0))


@pytest.fixture
def conn():
    return FakeConnection()


@pytest.fixture
async def live(hass, conn, quick, aioclient_mock):
    made = OvenLiveStream(hass, conn, async_get_clientsession(hass))
    yield made
    made.end_all()  # no timers left behind


def answers(aioclient_mock, *statuses, location=RESOURCE):
    """The WHEP link answers each offer with the next status (the mock alone repeats the first)."""
    queue = list(statuses)

    async def next_answer(method, url, data):
        status = queue.pop(0)
        headers = {"Location": location} if (location and status == 201) else {}
        return AiohttpClientMockResponse(
            method, url, status=status, text="answer-sdp" if status == 201 else "busy", headers=headers
        )

    aioclient_mock.post(LINK, side_effect=next_answer)
    aioclient_mock.delete(RESOURCE, status=200)


def posts(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if c[0] == "POST"]


def deletes(aioclient_mock):
    return [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "DELETE"]


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_two_viewers_one_start(live, conn, aioclient_mock):
    answers(aioclient_mock, 201, 201)
    first = await live.watch("phone", "offer-1")
    second = await live.watch("tablet", "offer-2")
    assert first == second == "answer-sdp"
    assert conn.sent == [(START, None)]
    assert len(posts(aioclient_mock)) == 2


async def test_offer_sent_as_sdp(live, aioclient_mock):
    answers(aioclient_mock, 201)
    await live.watch("phone", "v=0 offer")
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url), data) == ("POST", LINK, "v=0 offer")
    assert headers["Content-Type"] == "application/sdp"


async def test_keep_alive_every_60s(hass, live, conn, aioclient_mock, freezer):
    answers(aioclient_mock, 201)
    await live.watch("phone", "offer")
    await advance(hass, freezer, 60)
    await advance(hass, freezer, 60)
    assert conn.count(START) == 3
    await live.leave("phone")
    await advance(hass, freezer, 60)
    assert conn.count(START) == 3


async def test_stop_60s_after_last_viewer(hass, live, conn, aioclient_mock, freezer):
    answers(aioclient_mock, 201, 201)
    await live.watch("phone", "offer")
    await live.watch("tablet", "offer")
    await live.leave("phone")
    await advance(hass, freezer, 70)
    assert conn.count(STOP) == 0
    await live.leave("tablet")
    await advance(hass, freezer, 59)
    assert conn.count(STOP) == 0
    await advance(hass, freezer, 1)
    assert conn.sent[-1] == (STOP, None)
    assert conn.count(STOP) == 1


async def test_return_within_60s_keeps_stream(hass, live, conn, aioclient_mock, freezer):
    answers(aioclient_mock, 201, 201)
    await live.watch("phone", "offer")
    await live.leave("phone")
    await advance(hass, freezer, 30)
    await live.watch("phone", "offer")
    await advance(hass, freezer, 40)
    assert conn.count(STOP) == 0


async def test_leave_deletes_the_viewers_session(live, aioclient_mock):
    answers(aioclient_mock, 201)
    await live.watch("phone", "offer")
    await live.leave("phone")
    assert deletes(aioclient_mock) == [RESOURCE]


async def test_no_location_header(live, aioclient_mock):
    answers(aioclient_mock, 201, location=None)
    assert await live.watch("phone", "offer") == "answer-sdp"
    await live.leave("phone")
    assert deletes(aioclient_mock) == []


async def test_offer_retried_after_409(live, aioclient_mock):
    answers(aioclient_mock, 409, 409, 201)
    assert await live.watch("phone", "offer") == "answer-sdp"
    assert len(posts(aioclient_mock)) == 3


async def test_offer_refused_four_times(hass, live, conn, aioclient_mock, freezer):
    answers(aioclient_mock, 409, 409, 409, 409)
    with pytest.raises(CameraFailed) as err:
        await live.watch("phone", "offer")
    assert "https://" not in str(err.value)
    assert len(posts(aioclient_mock)) == 4
    await advance(hass, freezer, 60)
    assert conn.count(STOP) == 1  # nobody watches the stream the oven started


async def test_first_offer_waits_for_the_stream(hass, conn, aioclient_mock, monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(stream_module, "_sleep", fake_sleep)
    live = OvenLiveStream(hass, conn, async_get_clientsession(hass))
    answers(aioclient_mock, 201, 201)
    await live.watch("phone", "offer")
    await live.watch("tablet", "offer")
    live.end_all()
    assert waits == [3]


async def test_reply_without_link(live, conn):
    conn.replies = [{"status": "ok"}]
    with pytest.raises(CameraFailed):
        await live.watch("phone", "offer")


@pytest.mark.parametrize("error", [OvenRefused("unauthorized"), OvenOffline("Connection lost")])
async def test_start_refused(live, conn, error):
    conn.replies = [error]
    with pytest.raises(CameraFailed) as err:
        await live.watch("phone", "offer")
    assert str(err.value) == "The oven's camera did not start"


async def test_unreachable_link(live, aioclient_mock):
    aioclient_mock.post(LINK, exc=asyncio.TimeoutError())
    with pytest.raises(CameraFailed):
        await live.watch("phone", "offer")


async def test_keep_alive_failure_continues(hass, live, conn, aioclient_mock, freezer):
    answers(aioclient_mock, 201)
    await live.watch("phone", "offer")
    conn.replies = [OvenOffline("Connection lost")]
    await advance(hass, freezer, 60)
    await advance(hass, freezer, 60)
    assert conn.count(START) == 3


async def test_end_all_sends_nothing(hass, live, conn, aioclient_mock, freezer):
    answers(aioclient_mock, 201)
    await live.watch("phone", "offer")
    live.end_all()
    await advance(hass, freezer, 180)
    assert conn.sent == [(START, None)]
    assert not live.watching


async def test_shutdown_sends_one_stop(live, conn, aioclient_mock):
    answers(aioclient_mock, 201)
    await live.watch("phone", "offer")
    await live.shutdown()
    assert conn.count(STOP) == 1
    assert not live.watching


async def test_shutdown_without_stream_sends_nothing(live, conn):
    await live.shutdown()
    assert conn.sent == []


async def test_shutdown_gives_up_after_its_limit(hass, conn, aioclient_mock, quick, monkeypatch):
    monkeypatch.setattr(stream_module, "STOP_TIMEOUT", 0.05)
    live = OvenLiveStream(hass, conn, async_get_clientsession(hass))
    answers(aioclient_mock, 201)
    await live.watch("phone", "offer")
    never = asyncio.Event()

    async def hangs(command, payload, timeout=10):
        await never.wait()

    conn.send = hangs
    async with asyncio.timeout(1):
        await live.shutdown()


async def test_link_never_logged(hass, live, conn, aioclient_mock, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.jelly_anova")
    answers(aioclient_mock, 409, 201)
    await live.watch("phone", "offer")
    await advance(hass, freezer, 60)
    await live.leave("phone")
    await advance(hass, freezer, 60)
    assert "Live video started" in caplog.text
    assert "Live video stop sent" in caplog.text
    assert "whep.example" not in caplog.text
