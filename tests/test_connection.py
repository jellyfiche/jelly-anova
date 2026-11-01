"""Tests for the websocket connection, against a fake websocket. No network."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest

from custom_components.jelly_anova import connection
from custom_components.jelly_anova.auth import AuthFailed, CannotConnect, InvalidApiKey
from custom_components.jelly_anova.connection import (
    OvenConnection,
    OvenNoReply,
    OvenOffline,
    OvenRefused,
    backup_stop,
)

COOKER = "cooker-abc"
SECRET = "SECRET-TOKEN-VALUE"  # leak-check: allow (fake test value)
OLD = "2000-01-01T00:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"


def report(stamp: str, mode: str = "idle") -> dict:
    return {
        "command": "EVENT_APO_STATE",
        "payload": {"state": {"updatedTimestamp": stamp, "state": {"mode": mode}, "nodes": {}}},
    }


WIFI = {"command": "EVENT_APO_WIFI_LIST", "payload": [{"cookerId": COOKER}]}


def msg(kind, data=None):
    return aiohttp.WSMessage(kind, data, None)


class FakeWS:
    def __init__(self, first=(WIFI,), reply=None):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.reply = reply  # callable(sent dict) -> dict | None
        for item in first:
            self.push(item)

    def push(self, item):
        self.queue.put_nowait(msg(aiohttp.WSMsgType.TEXT, json.dumps(item)))

    def end(self):
        self.queue.put_nowait(msg(aiohttp.WSMsgType.CLOSED))

    async def receive(self):
        return await self.queue.get()

    async def send_str(self, text):
        sent = json.loads(text)
        self.sent.append(sent)
        if self.reply:
            answer = self.reply(sent)
            if answer is not None:
                self.push(answer)

    async def close(self):
        self.closed = True
        self.end()


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)  # each a FakeWS or an exception
        self.calls: list[tuple] = []

    async def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class FakeAuth:
    def __init__(self, error=None):
        self.n = 0
        self.error = error

    async def access_token(self):
        if self.error:
            raise self.error
        self.n += 1
        return f"{SECRET}-{self.n}"


async def until(check, tries=500):
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def make(session, auth=None, sleeps=None, **kwargs):
    states, status, failed = [], [], []

    async def fake_sleep(delay):
        if sleeps is not None:
            sleeps.append(delay)
        await asyncio.sleep(0)

    conn = OvenConnection(
        session,
        auth or FakeAuth(),
        states.append,
        status.append,
        on_auth_failed=lambda: failed.append(1),
        sleep=fake_sleep,
        **kwargs,
    )
    conn.states, conn.status, conn.failed = states, status, failed
    return conn


async def started(conn):
    task = asyncio.create_task(conn.run())
    return task


async def stop(conn, task):
    await conn.close()
    await asyncio.wait_for(task, 2)


async def test_url_and_subprotocol():
    ws = FakeWS()
    session = FakeSession(ws)
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    url, kwargs = session.calls[0]
    assert url == (
        f"wss://devices.anovaculinary.io?token={SECRET}-1&supportedAccessories=APO&platform=ios"
    )
    assert kwargs["protocols"] == ("ANOVA_V2",)
    assert kwargs["heartbeat"] == 20
    await stop(conn, task)


async def test_cooker_id_from_wifi_list():
    conn = make(FakeSession(FakeWS()))
    assert conn.cooker_id is None
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    assert conn.cooker_id == COOKER
    assert conn.status == [True]
    await stop(conn, task)


async def test_stale_first_report_dropped():
    ws = FakeWS(first=(WIFI, report(OLD, "cook"), report(FUTURE, "idle"), report(OLD)))
    conn = make(FakeSession(ws))
    task = await started(conn)
    await until(lambda: conn.states)
    await until(lambda: ws.queue.empty())
    await asyncio.sleep(0)
    # The cached old report is dropped; the live one passes; an older one after it is dropped.
    assert [s.mode for s in conn.states] == ["idle"]
    await stop(conn, task)


async def test_reconnect_delay_grows_to_300s():
    sleeps = []
    session = FakeSession(OSError("x"))
    conn = make(session, sleeps=sleeps)
    task = await started(conn)
    await until(lambda: len(sleeps) >= 10)
    assert sleeps[:8] == [5, 10, 20, 40, 80, 160, 300, 300]
    assert max(sleeps) == 300
    await stop(conn, task)


async def test_new_token_after_drop():
    first, second = FakeWS(), FakeWS()
    session = FakeSession(first, second)
    conn = make(session)
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    first.end()
    await until(lambda: len(session.calls) == 2)
    assert f"token={SECRET}-1&" in session.calls[0][0]
    assert f"token={SECRET}-2&" in session.calls[1][0]
    assert conn.status[:2] == [True, False]
    await stop(conn, task)


def ok(sent):
    return {"command": "RESPONSE", "requestId": sent["requestId"], "payload": {"status": "ok"}}


async def test_send_returns_response():
    ws = FakeWS(reply=ok)
    conn = make(FakeSession(ws))
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    answer = await conn.send("CMD_APO_STOP", None)
    assert answer == {"status": "ok"}
    sent = ws.sent[0]
    assert sent["command"] == "CMD_APO_STOP"
    assert sent["payload"] == {"id": COOKER, "type": "CMD_APO_STOP"}
    await conn.send("CMD_APO_START", {"stages": []})
    assert ws.sent[1]["payload"]["payload"] == {"stages": []}
    await stop(conn, task)


async def test_send_refused_raises():
    def refuse(sent):
        return {
            "command": "RESPONSE",
            "requestId": sent["requestId"],
            "payload": {"status": "error", "error": "bad stage"},
        }

    conn = make(FakeSession(FakeWS(reply=refuse)))
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    with pytest.raises(OvenRefused, match="bad stage"):
        await conn.send("CMD_APO_START", {})
    await stop(conn, task)


async def test_send_no_reply_raises():
    conn = make(FakeSession(FakeWS()))
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    with pytest.raises(OvenNoReply):
        await conn.send("CMD_APO_STOP", None, timeout=0.05)
    await stop(conn, task)


async def test_send_when_down_tries_one_reconnect():
    # Not connected: send opens the connection itself, once, then sends.
    ws = FakeWS(reply=ok)
    session = FakeSession(ws)
    conn = make(session)
    assert await conn.send("CMD_APO_STOP", None) == {"status": "ok"}
    assert len(session.calls) == 1
    await conn.close()
    # Still down and the reconnect fails: one try, then OvenOffline.
    session = FakeSession(OSError("x"))
    conn = make(session)
    with pytest.raises(OvenOffline):
        await conn.send("CMD_APO_STOP", None)
    assert len(session.calls) == 1
    await conn.close()


async def test_auth_failure_calls_back_once_and_stops():
    for error in (AuthFailed("x"), InvalidApiKey("x")):
        session = FakeSession(FakeWS())
        conn = make(session, auth=FakeAuth(error))
        await asyncio.wait_for(conn.run(), 2)
        assert conn.failed == [1]
        assert session.calls == []
        with pytest.raises(OvenOffline):
            await conn.send("CMD_APO_STOP", None)
        assert conn.failed == [1]


async def test_cannot_connect_retries():
    sleeps = []
    conn = make(FakeSession(FakeWS()), auth=FakeAuth(CannotConnect("x")), sleeps=sleeps)
    task = await started(conn)
    await until(lambda: len(sleeps) >= 3)
    assert conn.failed == []
    await stop(conn, task)


async def test_error_logs_hold_no_token(caplog):
    caplog.set_level(logging.DEBUG)
    leak = OSError(f"cannot reach wss://devices.anovaculinary.io?token={SECRET}-1")
    session = FakeSession(leak)
    conn = make(session, sleeps=[])
    task = await started(conn)
    await until(lambda: len(session.calls) >= 3)
    with pytest.raises(OvenOffline):
        await conn.send("CMD_APO_STOP", None)
    await stop(conn, task)
    text = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert SECRET not in text
    assert "devices.anovaculinary.io" not in text
    assert all(r.exc_info is None for r in caplog.records)


async def test_backup_stop_uses_personal_token():
    ws = FakeWS(first=(), reply=ok)
    session = FakeSession(ws)
    await backup_stop(session, "PERSONAL", COOKER)
    url, kwargs = session.calls[0]
    assert url == "wss://devices.anovaculinary.io?token=PERSONAL&supportedAccessories=APO"
    assert "protocols" not in kwargs
    assert ws.sent[0]["command"] == "CMD_APO_STOP"
    assert ws.sent[0]["payload"] == {"id": COOKER, "type": "CMD_APO_STOP"}
    assert ws.closed


async def test_backup_stop_failures(monkeypatch):
    monkeypatch.setattr(connection, "BACKUP_WAIT", 0.05)
    with pytest.raises(OvenNoReply):
        await backup_stop(FakeSession(FakeWS(first=())), "P", COOKER)
    with pytest.raises(OvenOffline):
        await backup_stop(FakeSession(OSError("x")), "P", COOKER)
    refuse = lambda s: {  # noqa: E731
        "command": "RESPONSE",
        "requestId": s["requestId"],
        "payload": {"status": "error", "error": "no"},
    }
    with pytest.raises(OvenRefused):
        await backup_stop(FakeSession(FakeWS(first=(), reply=refuse)), "P", COOKER)
