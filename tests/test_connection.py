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


async def test_ensure_open_opens_once_and_stays_quiet_when_open():
    ws = FakeWS()
    session = FakeSession(ws)
    conn = make(session)
    await conn.ensure_open()
    await conn.ensure_open()
    assert len(session.calls) == 1
    await conn.close()


async def test_ensure_open_down_is_offline_after_one_try():
    session = FakeSession(CannotConnect("x"))
    conn = make(session)
    with pytest.raises(OvenOffline):
        await conn.ensure_open()
    assert len(session.calls) == 1


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


async def test_a_socket_with_no_reports_for_three_minutes_is_reopened(monkeypatch):
    assert connection.REPORT_SILENCE == 180
    monkeypatch.setattr(connection, "REPORT_SILENCE", 0.05)
    quiet, next_one = FakeWS(), FakeWS()
    session = FakeSession(quiet, next_one)
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    quiet.push(report(FUTURE))
    await asyncio.wait_for(wait_until(lambda: len(session.calls) == 2), 2)
    assert quiet.closed
    assert conn.status[:2] == [True, False]
    await stop(conn, task)


async def test_only_live_reports_keep_a_socket_open(monkeypatch):
    monkeypatch.setattr(connection, "REPORT_SILENCE", 0.05)
    ws = FakeWS()
    session = FakeSession(ws, FakeWS())
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    for _ in range(10):  # a cached report and other messages are not reports from the oven
        ws.push(report(OLD))
        ws.push(WIFI)
        await asyncio.sleep(0.01)
    await asyncio.wait_for(wait_until(lambda: len(session.calls) >= 2), 2)
    assert conn.states == []
    await stop(conn, task)


async def test_live_reports_hold_the_socket_open(monkeypatch):
    monkeypatch.setattr(connection, "REPORT_SILENCE", 0.5)
    ws = FakeWS()
    session = FakeSession(ws)
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    for second in range(10, 20):
        ws.push(report(f"2999-01-01T00:00:{second}Z"))
        await asyncio.sleep(0.1)  # two seconds in all, each gap under the limit
    assert len(session.calls) == 1 and not ws.closed
    await stop(conn, task)


async def wait_until(check):
    while not check():
        await asyncio.sleep(0.005)


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


# --- fix round 1 -------------------------------------------------------------


class HungSession:
    """ws_connect never returns until the gate opens."""

    def __init__(self, ws=None):
        self.gate = asyncio.Event()
        self.ws = ws
        self.calls = []

    async def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        await self.gate.wait()
        return self.ws


async def test_close_during_connect_leaves_nothing_open():
    ws = FakeWS()
    session = HungSession(ws)
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    await asyncio.wait_for(conn.close(), 1)  # must not wait for the hung connect
    session.gate.set()
    await asyncio.wait_for(task, 1)
    assert conn.status == []
    assert conn._ws is None


async def test_close_when_connect_finishes_just_as_it_closes():
    ws = FakeWS()
    conn = make(FakeSession(ws))
    conn._closing = True  # close() won the race after ws_connect returned
    with pytest.raises(OvenOffline):
        await conn._open()
    assert conn._ws is None


async def test_send_with_hung_connect_gives_up_in_time():
    session = HungSession()
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    with pytest.raises(OvenOffline):
        await asyncio.wait_for(conn.send("CMD_APO_STOP", None, timeout=0.05), 1)
    await stop(conn, task)


async def test_backup_stop_hung_connect_gives_up(monkeypatch):
    monkeypatch.setattr(connection, "CONNECT_TIMEOUT", 0.05)
    with pytest.raises(OvenOffline):
        await asyncio.wait_for(backup_stop(HungSession(), "P", COOKER), 1)


async def test_ws_connect_gets_a_bounded_timeout():
    session = FakeSession(FakeWS())
    conn = make(session)
    task = await started(conn)
    await until(lambda: session.calls)
    assert session.calls[0][1]["timeout"] is not None
    await stop(conn, task)


async def test_backup_stop_ignores_foreign_response():
    def reply(sent):
        ws.push({"command": "RESPONSE", "requestId": "not-mine", "payload": {"status": "error", "error": "x"}})
        return ok(sent)

    ws = FakeWS(first=(), reply=reply)
    await backup_stop(FakeSession(ws), "P", COOKER)


async def test_backup_stop_only_foreign_response_gets_no_reply(monkeypatch):
    monkeypatch.setattr(connection, "BACKUP_WAIT", 0.05)

    def reply(sent):
        return {"command": "RESPONSE", "requestId": "not-mine", "payload": {"status": "ok"}}

    with pytest.raises(OvenNoReply):
        await backup_stop(FakeSession(FakeWS(first=(), reply=reply)), "P", COOKER)


async def test_unexpected_error_does_not_end_run():
    class Boom:
        calls = 0

        async def access_token(self):
            Boom.calls += 1
            if Boom.calls < 3:
                raise RuntimeError("bug")
            return "t"

    session = FakeSession(FakeWS())
    conn = make(session, auth=Boom())
    task = await started(conn)
    await until(lambda: session.calls)
    assert not task.done()
    await stop(conn, task)


async def test_bad_callbacks_do_not_end_run_or_leak():
    ws = FakeWS(first=(WIFI, report(FUTURE)))

    def bad(*_):
        raise RuntimeError("callback bug")

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0)

    conn = OvenConnection(FakeSession(ws), FakeAuth(), bad, bad, on_auth_failed=bad, sleep=fake_sleep)
    task = asyncio.create_task(conn.run())
    await until(lambda: ws.queue.empty())
    await asyncio.sleep(0.01)
    assert not task.done()
    assert not ws.closed
    ws.push(report("2999-06-01T00:00:00Z"))
    await asyncio.sleep(0.01)
    assert not task.done()
    await conn.close()
    await asyncio.wait_for(task, 1)


async def test_junk_messages_leave_run_alive():
    ws = FakeWS(first=(WIFI,))
    conn = make(FakeSession(ws))
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    ws.queue.put_nowait(msg(aiohttp.WSMsgType.TEXT, "not json {"))
    ws.queue.put_nowait(msg(aiohttp.WSMsgType.TEXT, "[1, 2]"))
    ws.queue.put_nowait(msg(aiohttp.WSMsgType.TEXT, '"text"'))
    ws.push({"command": "EVENT_APO_STATE", "payload": "junk"})
    ws.push({"command": "EVENT_APO_STATE", "payload": {"state": {"nodes": 5, "updatedTimestamp": 7}}})
    ws.push({"command": "EVENT_APO_WIFI_LIST", "payload": "x"})
    ws.push(report(FUTURE))
    await until(lambda: conn.states)
    assert not task.done()
    assert not ws.closed
    await stop(conn, task)


async def test_pending_send_fails_when_connection_drops():
    ws = FakeWS()
    conn = make(FakeSession(ws, FakeWS()))
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    sending = asyncio.create_task(conn.send("CMD_APO_STOP", None, timeout=5))
    await until(lambda: ws.sent)
    ws.end()
    with pytest.raises(OvenOffline):
        await asyncio.wait_for(sending, 1)
    await stop(conn, task)


async def test_pending_send_fails_when_ws_is_replaced():
    old = FakeWS()
    conn = make(FakeSession(old, FakeWS()))
    await conn._open()
    await until(lambda: conn.cooker_id)
    sending = asyncio.create_task(conn.send("CMD_APO_STOP", None, timeout=5))
    await until(lambda: old.sent)
    old.closed = True  # closed, but its reader has not noticed yet
    await conn._open()
    with pytest.raises(OvenOffline):
        await asyncio.wait_for(sending, 1)
    await conn.close()


async def test_close_during_send_fails_it():
    ws = FakeWS()
    conn = make(FakeSession(ws))
    task = await started(conn)
    await until(lambda: conn.cooker_id)
    sending = asyncio.create_task(conn.send("CMD_APO_STOP", None, timeout=5))
    await until(lambda: ws.sent)
    await conn.close()
    with pytest.raises(OvenOffline):
        await asyncio.wait_for(sending, 1)
    await asyncio.wait_for(task, 1)


async def test_login_failure_logs_one_plain_warning(caplog):
    caplog.set_level(logging.WARNING)
    conn = make(FakeSession(FakeWS()), auth=FakeAuth(AuthFailed(SECRET)))
    await asyncio.wait_for(conn.run(), 1)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert SECRET not in caplog.text


async def test_reader_closes_ws_when_it_ends_on_its_own():
    ws = FakeWS()
    conn = make(FakeSession(ws))
    await conn._open()
    ws.end()
    await until(lambda: conn._ws is None)
    await asyncio.wait([conn._reader])
    assert ws.closed
    await conn.close()
