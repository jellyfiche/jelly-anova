"""Tests for the runtime: cook limits and stops. A fake connection stands in for the oven."""

import asyncio
from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.jelly_anova import runtime as runtime_module
from custom_components.jelly_anova.connection import OvenNoReply, OvenOffline, OvenRefused
from custom_components.jelly_anova.const import DOMAIN, EVENT_COOK_FINISHED, EVENT_TARGET_REACHED
from custom_components.jelly_anova.cook import CookError, CookRequest, build_start
from custom_components.jelly_anova.runtime import OvenRuntime
from custom_components.jelly_anova.state import parse_state

STOP = "CMD_APO_STOP"
START = "CMD_APO_START"
UPDATE = "CMD_APO_UPDATE_COOK_STAGE"
OTHER = "ios-someone-elses-cook"
TIMED = build_start(CookRequest(temperature_f=350, minutes=30), "x")[0]["stages"]
ONE_STAGE = TIMED[:1]
NO_TIMER = build_start(CookRequest(temperature_c=100), "x")[0]["stages"]


def report(mode="cook", cook_id=None, stages=None, index=0, stage_mode="running", timer=None, dry=None, wet=None, at=None, timer_started=None):
    """An oven report; idle reports carry no cook section, as on the real oven."""
    nodes = {
        "temperatureBulbs": {
            "mode": "dry",
            "dry": {"current": {"celsius": dry}},
            "wet": {"current": {"celsius": wet}},
        }
    }
    if timer is not None:
        nodes["timer"] = {"mode": timer}
        if timer_started is not None:
            nodes["timer"]["startedAtTimestamp"] = timer_started.isoformat()
    raw = {
        "updatedTimestamp": (at or dt_util.utcnow()).isoformat(),
        "state": {"mode": mode},
        "nodes": nodes,
    }
    if mode != "idle":
        raw["cook"] = {
            "cookId": cook_id,
            "originSource": "ios",
            "activeStageIndex": index,
            "activeStageMode": stage_mode,
            "stages": NO_TIMER if stages is None else stages,
        }
    return parse_state({"state": raw}, None)


class FakeConnection:
    """Records commands. `errors` are raised by the next sends, in order."""

    def __init__(self):
        self.cooker_id = "cooker-abc"
        self.sent = []
        self.errors = []
        self.on_send = None
        self.gate = None  # an asyncio.Event a stop waits on after it is recorded
        self.opens = 0
        self.open_error = None
        self.feed = None  # the runtime's on_state, set by Rig.boot
        self.shows_start = True  # False: the oven answers ok and ignores the start

    async def ensure_open(self, timeout=10):
        self.opens += 1
        if self.open_error:
            raise self.open_error

    async def send(self, command, payload, timeout=10):
        self.sent.append((command, payload))
        if self.on_send:
            self.on_send(command)
        if self.gate and command == STOP:
            await self.gate.wait()
        if self.errors:
            raise self.errors.pop(0)
        if command == START and self.shows_start and self.feed:
            self.feed(report("cook", payload["cookId"], stages=payload["stages"]))
        return {"status": "ok"}

    def count(self, command):
        return sum(1 for sent, _ in self.sent if sent == command)

    @property
    def order(self):
        return [sent for sent, _ in self.sent]


class Rig:
    def __init__(self, hass, storage, token="personal-token"):
        self.hass = hass
        self.storage = storage
        self.entry = MockConfigEntry(domain=DOMAIN, data={"personal_token": token})
        self.entry.add_to_hass(hass)
        self.conn = FakeConnection()
        self.runtimes = []
        self.backups = []

    async def boot(self):
        """A new runtime on the same store, as after a Home Assistant restart."""
        rt = OvenRuntime(self.hass, self.entry, self.conn, object())
        self.runtimes.append(rt)
        self.conn.feed = rt.on_state
        await rt.restore()
        rt.on_status(True)
        return rt

    @property
    def key(self):
        return f"{DOMAIN}.{self.entry.entry_id}"

    @property
    def saved(self):
        return self.storage[self.key]["data"]

    def seed(self, **data):
        self.storage[self.key] = {"version": 1, "minor_version": 1, "key": self.key, "data": data}

    async def settle(self):
        await self.hass.async_block_till_done(wait_background_tasks=True)

    async def advance(self, freezer, seconds):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass)
        await self.settle()

    async def close(self):
        for rt in self.runtimes:
            await rt.shutdown()


@pytest.fixture
async def rig(hass, hass_storage, monkeypatch):
    async def fake_backup(session, token, cooker):
        made.backups.append((token, cooker))
        if made.backup_error:
            raise made.backup_error

    async def found_key(session):
        return "web-key"

    monkeypatch.setattr(runtime_module, "backup_stop", fake_backup)
    monkeypatch.setattr("custom_components.jelly_anova.config_flow.find_api_key", found_key)
    made = Rig(hass, hass_storage)
    made.backup_error = None
    yield made
    await made.close()


async def started(rig, req=None, freshen=True, shown=True):
    """A runtime that has just started a cook on an idle oven.

    With shown=False the oven has answered ok and no report has shown the cook yet.
    """
    rt = await rig.boot()
    if freshen:
        rt.on_state(report("idle"))
    rig.conn.shows_start = shown
    pending = asyncio.ensure_future(rt.start(req or CookRequest(temperature_f=350)))
    await rig.settle()
    if shown:
        return rt, await pending
    pending.cancel()
    return rt, rig.saved["cook_id"]


# --- starting ---------------------------------------------------------------


async def test_start_saves_limit(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    assert rig.saved["cook_id"] == cook_id
    expected = dt_util.utcnow() + timedelta(minutes=40)  # limit_minutes: timer plus margin
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == expected
    assert rig.conn.count(START) == 1


async def test_start_limit_saved_before_the_command_goes_out(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    seen = []
    rig.conn.on_send = lambda command: seen.append(rig.storage[rig.key]["data"]["cook_id"])
    cook_id = await rt.start(CookRequest(temperature_f=350))
    assert seen == [cook_id]


async def test_start_no_timer_gets_default_limit(rig, freezer):
    await started(rig)
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=180)


async def test_start_during_cook_stops_first(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))

    def goes_idle(command):
        if command == STOP:
            later = dt_util.utcnow() + timedelta(seconds=1)
            rig.hass.loop.call_soon(rt.on_state, report("idle", at=later))

    rig.conn.on_send = goes_idle
    cook_id = await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.order == [STOP, START]
    assert cook_id.startswith("ios-")


async def test_start_during_cook_ignores_idle_report_from_before_the_stop(rig, monkeypatch):
    monkeypatch.setattr(runtime_module, "IDLE_WAIT", 0.05)
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    earlier = dt_util.utcnow() - timedelta(seconds=5)
    rig.conn.on_send = lambda c: rig.hass.loop.call_soon(rt.on_state, report("idle", at=earlier))
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.count(START) == 0


async def test_start_during_cook_never_idle_raises_and_does_not_start(rig, monkeypatch):
    monkeypatch.setattr(runtime_module, "IDLE_WAIT", 0.05)
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    with pytest.raises(HomeAssistantError) as err:
        await rt.start(CookRequest(temperature_f=350))
    assert "stop" in str(err.value).lower()
    assert rig.conn.order == [STOP]


async def test_start_without_a_fresh_report_stops_nothing_and_starts_nothing(rig, monkeypatch):
    monkeypatch.setattr(runtime_module, "REPORT_WAIT", 0.05)
    rt = await rig.boot()
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.sent == []


async def test_start_waits_for_a_fresh_report(rig):
    rt = await rig.boot()
    rig.hass.loop.call_soon(rt.on_state, report("idle"))
    await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.order == [START]


async def test_bad_request_leaves_running_cook_alone(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    with pytest.raises(CookError):
        await rt.start(CookRequest(temperature_f=999))
    assert rig.conn.sent == []


async def test_max_minutes_below_one_refused_before_anything_is_sent(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    with pytest.raises(CookError):
        await rt.start(CookRequest(temperature_f=350, max_minutes=0))
    assert rig.conn.sent == []


async def test_start_refused_leaves_no_limit(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.errors = [OvenRefused("no")]
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    await rig.settle()
    assert rig.saved["cook_id"] is None
    assert rig.saved["stop_at"] is None


@pytest.mark.parametrize("error", [OvenNoReply("x"), OvenOffline("x")])
async def test_start_unanswered_keeps_limit_as_it_may_have_arrived(rig, error):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.errors = [error]
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    await rig.settle()
    assert rig.saved["cook_id"] is not None
    assert rig.saved["stop_at"] is not None


async def test_stop_pressed_while_start_waits_cancels_the_start(rig):
    rt = await rig.boot()
    pending = asyncio.ensure_future(rt.start(CookRequest(temperature_f=350)))
    await asyncio.sleep(0)
    await rt.stop()
    rt.on_state(report("idle"))
    with pytest.raises(HomeAssistantError):
        await pending
    assert rig.conn.count(START) == 0


async def test_stop_pressed_while_the_start_saves_cancels_the_start(rig, monkeypatch):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    real_save, pressed = rt._store.async_save, []

    async def save_then_press(data):
        await real_save(data)
        if not pressed:
            pressed.append(True)
            await rt.stop()

    monkeypatch.setattr(rt._store, "async_save", save_then_press)
    with pytest.raises(HomeAssistantError, match="Stop was pressed"):
        await rt.start(CookRequest(temperature_f=350))
    await rig.settle()
    assert rig.conn.count(START) == 0
    assert rig.conn.count(STOP) == 1
    assert rig.saved["cook_id"] is None and rig.saved["stop_at"] is None


async def test_start_returns_only_once_a_report_shows_the_cook(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.shows_start = False
    pending = asyncio.ensure_future(rt.start(CookRequest(temperature_f=350)))
    await rig.settle()
    assert rig.conn.count(START) == 1 and not pending.done()
    assert not rt._start_lock.locked()  # waited outside the lock, so a stop or start can get in
    rt.on_state(report("cook", OTHER))  # someone else's cook is not this one
    await rig.settle()
    assert not pending.done()
    cook_id = rig.saved["cook_id"]
    rt.on_state(report("cook", cook_id))
    assert await pending == cook_id


async def test_start_ignored_by_the_oven_says_so_and_keeps_the_stop_time(rig, monkeypatch):
    assert runtime_module.START_SEEN_WAIT == 10
    monkeypatch.setattr(runtime_module, "START_SEEN_WAIT", 0.01)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.shows_start = False
    with pytest.raises(HomeAssistantError, match="accepted the cook but did not start it"):
        await rt.start(CookRequest(temperature_f=350))
    await rig.settle()
    assert rig.saved["cook_id"] and rig.saved["stop_at"]


async def test_start_custom_ignored_by_the_oven_says_so_and_keeps_the_stop_time(rig, monkeypatch):
    monkeypatch.setattr(runtime_module, "START_SEEN_WAIT", 0.01)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.shows_start = False
    with pytest.raises(HomeAssistantError, match="accepted the cook but did not start it"):
        await rt.start_custom(NO_TIMER, None)
    await rig.settle()
    assert rig.saved["cook_id"] and rig.saved["stop_at"]


async def test_start_not_connected_yet_is_a_plain_error(rig):
    rt = await rig.boot()
    rig.conn.cooker_id = None
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))


async def test_start_custom_max_minutes(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    cook_id = await rt.start_custom(NO_TIMER, 45)
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=45)
    command, payload = rig.conn.sent[-1]
    assert command == START
    assert payload["stages"] == NO_TIMER
    assert payload["cookId"] == cook_id
    assert payload["cookerId"] == "cooker-abc"
    assert payload["type"] == "oven_v2"


async def test_start_custom_default_180(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    await rt.start_custom(NO_TIMER, None)
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=180)


async def test_start_custom_refuses_no_stages_and_too_hot(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    with pytest.raises(CookError):
        await rt.start_custom([], None)
    with pytest.raises(CookError):
        await rt.start_custom([{"do": "nonsense"}], None)
    hot = build_start(CookRequest(temperature_c=100), "x")[0]["stages"]
    hot[0]["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] = 300
    with pytest.raises(CookError):
        await rt.start_custom(hot, None)
    assert rig.conn.sent == []


async def test_second_start_cannot_lose_the_first_cooks_stop_time(rig, monkeypatch):
    """Idle lags a start: start, start, then the first cook shows. A stop must still reach it."""
    monkeypatch.setattr(runtime_module, "IDLE_WAIT", 0.05)
    rt, first = await started(rig, CookRequest(temperature_f=350, max_minutes=1), shown=False)
    rt.on_state(report("idle"))
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=300))
    assert rig.conn.order == [START, STOP]
    await rig.settle()
    assert rig.saved["cook_id"] == first
    assert rig.saved["stop_at"] is not None


async def test_start_start_then_first_cook_shows_and_a_stop_reaches_it(rig, monkeypatch):
    """The whole of the mistake: the second start must not leave the first cook with no limit."""
    monkeypatch.setattr(runtime_module, "IDLE_WAIT", 0.05)
    rt, first = await started(rig, CookRequest(temperature_f=350, max_minutes=1), shown=False)
    rt.on_state(report("idle"))
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=300, max_minutes=1))
    rig.conn.sent.clear()
    with freeze_time(dt_util.utcnow() + timedelta(seconds=61)):
        rt.on_state(report("cook", first))
        async_fire_time_changed(rig.hass)
        await rig.settle()
    assert rig.conn.order == [STOP]
    assert rig.saved["cook_id"] == first


async def test_second_start_clears_the_problem_report_of_the_cook_it_ended(rig):
    rt, first = await started(rig)
    ir.async_create_issue(
        rig.hass, DOMAIN, "stop_failed", is_fixable=False,
        severity=ir.IssueSeverity.CRITICAL, translation_key="stop_failed",
    )

    def goes_idle(command):
        if command == STOP:
            later = dt_util.utcnow() + timedelta(seconds=1)
            rig.hass.loop.call_soon(rt.on_state, report("idle", at=later))

    rig.conn.on_send = goes_idle
    await rt.start(CookRequest(temperature_f=300))
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is None


async def test_second_start_stops_the_first_and_keeps_only_the_second(rig):
    rt, first = await started(rig)

    def goes_idle(command):
        if command == STOP:
            later = dt_util.utcnow() + timedelta(seconds=1)
            rig.hass.loop.call_soon(rt.on_state, report("idle", at=later))

    rig.conn.on_send = goes_idle
    second = await rt.start(CookRequest(temperature_f=300))
    await rig.settle()
    assert rig.conn.order == [START, STOP, START]
    assert rig.saved["cook_id"] == second


async def test_start_after_a_cleared_cook_needs_a_report_from_after_that_start(rig, monkeypatch):
    monkeypatch.setattr(runtime_module, "REPORT_WAIT", 0.05)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.errors = [OvenRefused("no")]
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    rig.conn.sent.clear()
    rt.on_state(report("idle", at=dt_util.utcnow() - timedelta(seconds=5)))
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.sent == []


async def test_two_starts_at_once_take_turns(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))

    def goes_idle(command):
        if command == STOP:
            later = dt_util.utcnow() + timedelta(seconds=1)
            rig.hass.loop.call_soon(rt.on_state, report("idle", at=later))

    rig.conn.on_send = goes_idle
    one, two = await asyncio.gather(
        rt.start(CookRequest(temperature_f=350)), rt.start(CookRequest(temperature_f=300))
    )
    await rig.settle()
    assert rig.conn.order == [START, STOP, START]
    assert rig.saved["cook_id"] == two != one


async def test_start_reconnects_once_when_the_socket_is_down(rig):
    rt = await rig.boot()
    rt.on_status(False)

    async def comes_back(timeout=10):
        rig.conn.opens += 1
        rt.on_status(True)
        rig.hass.loop.call_soon(rt.on_state, report("idle"))

    rig.conn.ensure_open = comes_back
    await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.opens == 1
    assert rig.conn.order == [START]


async def test_start_when_reconnect_fails_is_a_plain_error_at_once(rig):
    rt = await rig.boot()
    rt.on_status(False)
    rig.conn.open_error = OvenOffline("x")
    with pytest.raises(HomeAssistantError):
        await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.opens == 1
    assert rig.conn.sent == []


async def test_start_does_not_reconnect_when_connected(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    await rt.start(CookRequest(temperature_f=350))
    assert rig.conn.opens == 0


# --- the saved stop time ----------------------------------------------------


async def test_limit_passes_stops_cook(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=60))
    await rig.advance(freezer, 59 * 60)
    assert rig.conn.count(STOP) == 0
    await rig.advance(freezer, 61)  # no report since the start: the timer does it
    assert rig.conn.count(STOP) == 1


async def test_limit_stop_not_repeated_while_oven_confirms(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    await rig.advance(freezer, 61)
    assert rig.conn.count(STOP) == 1
    await rig.advance(freezer, 5)
    await rig.advance(freezer, 5)
    assert rig.conn.count(STOP) == 1


async def test_stop_answered_with_no_reports_is_bounded_and_told(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    issues = ir.async_get(rig.hass)
    for _ in range(3):
        await rig.advance(freezer, 61)
    assert rig.conn.count(STOP) == 3
    assert issues.async_get_issue(DOMAIN, "stop_failed") is None
    await rig.advance(freezer, 61)
    await rig.advance(freezer, 300)
    assert rig.conn.count(STOP) == 3
    assert issues.async_get_issue(DOMAIN, "stop_failed") is not None
    assert rig.saved["stop_at"] is not None


async def test_reports_returning_after_bounded_stops_send_it_again(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    for _ in range(4):
        await rig.advance(freezer, 61)
    assert rig.conn.count(STOP) == 3
    rt.on_state(report("cook", cook_id))
    await rig.advance(freezer, 61)
    assert rig.conn.count(STOP) == 4
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None
    freezer.tick(timedelta(seconds=1))
    rt.on_state(report("idle"))
    await rig.settle()
    assert rig.saved["stop_at"] is None
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is None


async def test_oven_that_answers_ok_but_keeps_cooking_is_bounded_and_told(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    rt.on_state(report("cook", cook_id))
    await rig.advance(freezer, 30)
    for _ in range(40):
        rt.on_state(report("cook", cook_id))
        await rig.advance(freezer, 30)
    assert rig.conn.count(STOP) == 3
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None
    assert rig.saved["stop_at"] is not None
    rt.on_state(report("idle"))
    await rig.settle()
    assert rig.saved["stop_at"] is None
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is None


async def test_cook_finishing_other_way_after_bounded_stops_is_not_stopped(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    for _ in range(3):
        await rig.advance(freezer, 61)
    await rig.advance(freezer, 55)
    rt.on_state(report("cook", OTHER))
    await rig.advance(freezer, 6)
    assert rig.conn.count(STOP) == 3
    assert rig.saved["stop_at"] is None


async def test_user_stop_answered_keeps_limit_until_cook_seen_gone(rig, freezer):
    rt, cook_id = await started(rig)
    await rt.stop()
    await rig.settle()
    assert rig.saved["stop_at"] is not None
    freezer.tick(timedelta(seconds=1))
    rt.on_state(report("idle"))
    await rig.settle()
    assert rig.saved["stop_at"] is None


async def test_limit_stop_sent_again_if_cook_keeps_reporting(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    rt.on_state(report("cook", cook_id))
    await rig.advance(freezer, 59)
    rt.on_state(report("cook", cook_id))
    await rig.advance(freezer, 6)
    assert rig.conn.count(STOP) == 1
    await rig.advance(freezer, 30)
    rt.on_state(report("cook", cook_id))
    await rig.advance(freezer, 31)
    assert rig.conn.count(STOP) == 2


async def test_limit_stop_undelivered_keeps_trying_and_raises_a_problem(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    rig.conn.errors = [OvenOffline("x"), OvenOffline("x")]
    rig.backup_error = OvenOffline("x")
    await rig.advance(freezer, 61)
    assert rig.saved["stop_at"] is not None
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None
    rig.backup_error = None
    await rig.advance(freezer, 61)
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None
    assert rig.saved["stop_at"] is not None  # answered, not yet seen
    freezer.tick(timedelta(seconds=1))
    rt.on_state(report("idle"))
    await rig.settle()
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is None
    assert rig.saved["stop_at"] is None


async def test_stop_problem_goes_when_a_report_shows_the_cook_ended(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    rt.on_state(report("cook", cook_id))
    rig.conn.errors = [OvenOffline("x"), OvenOffline("x")]
    rig.backup_error = OvenOffline("x")
    await rig.advance(freezer, 61)
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None
    rt.on_state(report("idle"))
    await rig.settle()
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is None


async def test_due_stop_stays_silent_after_shutdown(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    await rt.shutdown()
    await rig.advance(freezer, 61)
    rt.on_state(report("cook", cook_id))
    await rig.settle()
    assert rig.conn.count(STOP) == 0
    assert rig.saved["stop_at"] is not None  # for the next start-up


async def test_unexpected_error_still_reaches_backup(rig):
    rt = await rig.boot()
    rig.conn.errors = [KeyError("x"), KeyError("x")]
    await rt.stop()
    assert len(rig.backups) == 1


async def test_unexpected_backup_error_is_still_plain_and_raises_the_problem(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    rig.conn.errors = [KeyError("x"), KeyError("x")]
    rig.backup_error = KeyError("x")
    await rig.advance(freezer, 61)
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None


async def test_unexpected_error_inside_delivery_raises_the_problem(rig, freezer, monkeypatch):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))

    async def broken():
        raise KeyError("x")

    monkeypatch.setattr(rt, "_deliver_stop", broken)
    await rig.advance(freezer, 61)
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "stop_failed") is not None


async def test_null_setpoint_cannot_skip_the_stop(rig):
    stages = [dict(TIMED[0]), dict(TIMED[1], do=dict(TIMED[1]["do"], temperatureBulbs=None))]
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    rt.on_state(report("cook", cook_id, stages=stages, index=1, timer="running"))
    await rig.settle()
    assert rig.conn.count(STOP) == 1


@pytest.mark.parametrize("bulbs", [None, {"mode": "dry", "dry": None}, {"mode": "dry", "dry": {"setpoint": None}}, {"mode": None}])
async def test_null_setpoint_cannot_skip_a_timer_end_or_stop_listeners(rig, bulbs):
    stage = dict(ONE_STAGE[0], do=dict(ONE_STAGE[0]["do"], temperatureBulbs=bulbs))
    rt, cook_id = await started(rig)
    told = []
    rt.add_listener(lambda: told.append(1))
    rt.on_state(report("cook", cook_id, stages=[stage], timer="running", dry=50))
    rt.on_state(report("cook", cook_id, stages=[stage], timer="completed", dry=50))
    await rig.settle()
    assert rig.conn.count(STOP) == 1
    assert len(told) == 2


async def test_stop_checks_run_before_anything_that_could_fail(rig, monkeypatch):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))

    def broken(state):
        raise KeyError("x")

    monkeypatch.setattr(rt, "_check_target", broken)
    monkeypatch.setattr(rt, "_count_hours", broken)
    with pytest.raises(KeyError):  # the connection logs this and carries on
        rt.on_state(report("cook", cook_id, stages=TIMED, index=1))
    await rig.settle()
    assert rig.conn.count(STOP) == 1


async def test_limit_survives_restart(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=60))
    await rt.shutdown()
    rig.conn.sent.clear()
    after = await rig.boot()
    await rig.advance(freezer, 10 * 60)
    assert rig.conn.count(STOP) == 0
    after.on_state(report("cook", cook_id))
    await rig.advance(freezer, 51 * 60)
    assert rig.conn.count(STOP) == 1


async def test_restart_after_limit_stops(rig, freezer):
    past = dt_util.utcnow() - timedelta(minutes=5)
    rig.seed(cook_id="ios-ours", stop_at=past.isoformat(), total_cook_hours=1.0)
    await rig.boot()
    assert rig.conn.count(STOP) == 0  # first a chance to see what the oven is doing
    await rig.advance(freezer, 31)  # no report came: send it
    assert rig.conn.count(STOP) == 1


async def test_restart_after_limit_stops_on_first_report_of_that_cook(rig, freezer):
    past = dt_util.utcnow() - timedelta(minutes=5)
    rig.seed(cook_id="ios-ours", stop_at=past.isoformat(), total_cook_hours=1.0)
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-ours"))
    await rig.settle()
    assert rig.conn.count(STOP) == 1


async def test_restart_different_cook_left_alone(rig, freezer):
    past = dt_util.utcnow() - timedelta(minutes=5)
    rig.seed(cook_id="ios-ours", stop_at=past.isoformat(), total_cook_hours=1.0)
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    await rig.advance(freezer, 120)
    assert rig.conn.count(STOP) == 0
    assert rig.saved["cook_id"] is None


async def test_restart_due_stop_waits_for_a_first_report(rig, freezer):
    """The timer must not beat the first report: that could end a cook started since."""
    past = dt_util.utcnow() - timedelta(minutes=5)
    rig.seed(cook_id="ios-ours", stop_at=past.isoformat(), total_cook_hours=1.0)
    rt = await rig.boot()
    await rig.advance(freezer, 6)
    assert rig.conn.count(STOP) == 0
    rt.on_state(report("cook", OTHER))
    await rig.advance(freezer, 60)
    assert rig.conn.count(STOP) == 0


async def test_restart_idle_oven_clears_the_saved_cook(rig):
    future = dt_util.utcnow() + timedelta(minutes=5)
    rig.seed(cook_id="ios-ours", stop_at=future.isoformat(), total_cook_hours=1.0)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    await rig.settle()
    assert rig.saved["stop_at"] is None


async def test_limit_skipped_when_fresh_report_shows_other_cook(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1))
    await rig.advance(freezer, 59)
    rt.on_state(report("cook", OTHER))
    await rig.advance(freezer, 6)
    assert rig.conn.count(STOP) == 0
    assert rig.saved["stop_at"] is None


async def test_limit_sent_when_other_cook_report_is_stale(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=2), shown=False)
    await rig.advance(freezer, 10)
    rt.on_state(report("cook", OTHER))
    await rig.advance(freezer, 111)
    assert rig.conn.count(STOP) == 1


async def test_app_cook_gets_no_limit(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER, stages=NO_TIMER))
    await rig.advance(freezer, 5 * 3600)
    rt.on_state(report("cook", OTHER, stages=NO_TIMER))
    await rig.settle()
    assert rig.conn.sent == []
    assert rt.state.cook.cook_id == OTHER


async def test_limit_cleared_when_cook_ends(rig):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=60))
    rt.on_state(report("cook", cook_id))
    rt.on_state(report("idle"))
    await rig.settle()
    assert rig.saved["cook_id"] is None
    assert rig.saved["stop_at"] is None


async def test_idle_report_before_cook_is_seen_keeps_limit(rig):
    """Idle reports can lag a start; a limit must not be lost to one."""
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=60), shown=False)
    rt.on_state(report("idle"))
    await rig.settle()
    assert rig.saved["cook_id"] == cook_id
    assert rig.saved["stop_at"] is not None


async def test_cook_that_never_showed_is_cleared_at_its_limit(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1), shown=False)
    await rig.advance(freezer, 61)
    freezer.tick(timedelta(seconds=1))
    rt.on_state(report("idle"))
    await rig.advance(freezer, 5)
    assert rig.conn.count(STOP) == 1
    assert rig.saved["stop_at"] is None


async def test_idle_report_from_before_the_stop_does_not_clear_a_never_seen_cook(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, max_minutes=1), shown=False)
    before = dt_util.utcnow()
    await rig.advance(freezer, 61)
    rt.on_state(report("idle", at=before))
    await rig.settle()
    assert rig.saved["stop_at"] is not None


# --- a timed cook ending ----------------------------------------------------


async def test_cook_reaching_end_stage_is_stopped_at_once(rig):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    rt.on_state(report("cook", cook_id, stages=TIMED, index=1, timer="running"))
    await rig.settle()
    assert rig.conn.count(STOP) == 1


async def test_end_stage_stop_retried_after_restart(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    rig.conn.errors = [OvenOffline("x"), OvenOffline("x")]
    rig.backup_error = OvenOffline("x")
    rt.on_state(report("cook", cook_id, stages=TIMED, index=1, timer="running"))
    await rig.settle()
    await rt.shutdown()
    assert dt_util.parse_datetime(rig.saved["stop_at"]) <= dt_util.utcnow()
    rig.conn.sent.clear()
    rig.backup_error = None
    await rig.boot()
    await rig.advance(freezer, 31)
    assert rig.conn.count(STOP) == 1


@pytest.mark.parametrize("ended", ["idle", "completed"])
async def test_one_stage_timer_seen_running_then_ended_stops(rig, ended):
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer="running"))
    await rig.settle()
    assert rig.conn.count(STOP) == 0
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer=ended))
    await rig.settle()
    assert rig.conn.count(STOP) == 1


async def test_timer_never_seen_running_does_not_end_a_cook(rig):
    """A manual timer waits at idle; no stop until it has run."""
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer="idle"))
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer="idle"))
    await rig.settle()
    assert rig.conn.count(STOP) == 0


async def test_absent_timer_never_counts_as_ended(rig):
    """The mistake of 2026-11-01: "no timer" was read as "timer ended"."""
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id, stages=NO_TIMER, timer=None))
    rt.on_state(report("cook", cook_id, stages=NO_TIMER, timer="idle"))
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer="running"))
    rt.on_state(report("cook", cook_id, stages=NO_TIMER, timer=None))
    rt.on_state(report("cook", cook_id, stages=NO_TIMER, timer="idle"))
    await rig.settle()
    assert rig.conn.count(STOP) == 0


async def test_timer_running_seen_for_another_cook_does_not_count(rig):
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", OTHER, stages=ONE_STAGE, timer="running"))
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer="idle"))
    await rig.settle()
    assert rig.conn.count(STOP) == 0


async def test_app_cook_timer_ending_is_left_alone(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER, stages=ONE_STAGE, timer="running"))
    rt.on_state(report("cook", OTHER, stages=ONE_STAGE, timer="idle"))
    await rig.settle()
    assert rig.conn.sent == []


async def test_app_cook_in_a_stage_titled_like_ours_is_left_alone(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER, stages=TIMED, index=1))
    await rig.settle()
    assert rig.conn.sent == []


async def test_other_cook_reaching_a_last_stage_while_ours_is_saved_is_left_alone(rig):
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", OTHER, stages=TIMED, index=1))
    await rig.settle()
    assert rig.conn.count(STOP) == 0


async def test_custom_cook_of_two_stages_not_stopped_by_timer_change(rig):
    two = [dict(ONE_STAGE[0], title="a"), dict(ONE_STAGE[0], title="b")]
    rt = await rig.boot()
    rt.on_state(report("idle"))
    cook_id = await rt.start_custom(two, None)
    rt.on_state(report("cook", cook_id, stages=two, timer="running"))
    rt.on_state(report("cook", cook_id, stages=two, timer="completed"))
    await rig.settle()
    assert rig.conn.count(STOP) == 0


# --- stop -------------------------------------------------------------------


async def test_stop_no_id_always_sent_even_when_idle(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    await rt.stop()
    assert rig.conn.sent == [(STOP, None)]


async def test_stop_in_flight_leaves_a_cook_started_meanwhile_alone(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.gate = asyncio.Event()
    stopping = asyncio.ensure_future(rt.stop())
    await asyncio.sleep(0)
    rig.conn.shows_start = False
    starting = asyncio.ensure_future(rt.start(CookRequest(temperature_f=350)))
    await rig.settle()
    starting.cancel()
    cook_id = rig.saved["cook_id"]
    rig.conn.gate.set()
    await stopping
    await rig.settle()
    assert rig.saved["cook_id"] == cook_id
    assert rig.saved["stop_at"] is not None
    rt.on_state(report("idle"))  # lagging: this new cook has not shown yet
    await rig.settle()
    assert rig.saved["cook_id"] == cook_id


async def test_two_stops_at_once_both_go_and_neither_fails(rig):
    rt, cook_id = await started(rig)
    rig.conn.gate = asyncio.Event()
    both = asyncio.gather(rt.stop(), rt.stop(cook_id))
    await asyncio.sleep(0)
    rig.conn.gate.set()
    await both
    assert rig.conn.count(STOP) == 2
    assert rig.saved["stop_at"] is not None


async def test_stop_no_id_sent_with_no_report_at_all(rig):
    rt = await rig.boot()
    await rt.stop()
    assert rig.conn.count(STOP) == 1


async def test_stop_with_cook_id_skips_other_fresh_cook(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    await rt.stop("ios-earlier")
    assert rig.conn.sent == []


async def test_stop_with_cook_id_sent_for_that_cook(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-this"))
    await rt.stop("ios-this")
    assert rig.conn.count(STOP) == 1


async def test_stop_with_cook_id_sent_when_report_is_stale(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    freezer.tick(timedelta(seconds=61))
    await rt.stop("ios-earlier")
    assert rig.conn.count(STOP) == 1


async def test_stop_with_cook_id_sent_when_nothing_known(rig):
    rt = await rig.boot()
    await rt.stop("ios-earlier")
    assert rig.conn.count(STOP) == 1


@pytest.mark.parametrize("error", [OvenNoReply("x"), OvenOffline("x")])
async def test_stop_retried_once(rig, error):
    rt = await rig.boot()
    rig.conn.errors = [error]
    await rt.stop()
    assert rig.conn.count(STOP) == 2
    assert rig.backups == []


async def test_stop_falls_back_to_backup(rig):
    rt = await rig.boot()
    rig.conn.errors = [OvenNoReply("x"), OvenNoReply("x")]
    await rt.stop()
    assert rig.conn.count(STOP) == 2
    assert rig.backups == [("personal-token", "cooker-abc")]


async def test_backup_works_after_restart_with_login_gone(rig):
    """After a restart with a dead login the oven's id only comes from what was saved."""
    rt, cook_id = await started(rig)
    await rt.shutdown()
    rig.conn.cooker_id = None
    after = await rig.boot()
    rig.conn.errors = [OvenOffline("x"), OvenOffline("x")]
    await after.stop()
    assert rig.backups == [("personal-token", "cooker-abc")]


async def test_stop_no_backup_token_raises_plain_error(rig):
    rig.hass.config_entries.async_update_entry(rig.entry, data={"personal_token": ""})
    rt = await rig.boot()
    rig.conn.errors = [OvenNoReply("x"), OvenNoReply("x")]
    with pytest.raises(HomeAssistantError) as err:
        await rt.stop()
    assert "stop" in str(err.value).lower()
    assert rig.backups == []


async def test_stop_backup_failing_raises_plain_error(rig):
    rt = await rig.boot()
    rig.conn.errors = [OvenOffline("x"), OvenOffline("x")]
    rig.backup_error = OvenOffline("x")
    with pytest.raises(HomeAssistantError):
        await rt.stop()


async def test_stop_refused_on_idle_oven_is_not_an_error(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rig.conn.errors = [OvenRefused("nothing to stop")]
    await rt.stop()
    assert rig.conn.count(STOP) == 1
    assert rig.backups == []


async def test_stop_refused_during_a_cook_goes_on_to_backup(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-this"))
    rig.conn.errors = [OvenRefused("no"), OvenRefused("no")]
    await rt.stop()
    assert len(rig.backups) == 1


async def test_stop_refused_with_stale_idle_report_is_not_trusted(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    freezer.tick(timedelta(seconds=61))
    rig.conn.errors = [OvenRefused("no"), OvenRefused("no")]
    await rt.stop()
    assert len(rig.backups) == 1


# --- login ------------------------------------------------------------------


def login_flows(hass):
    return [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]


async def test_login_failure_during_cook_creates_issue_and_reauth(rig):
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id))
    rt.on_auth_failed()
    await rig.settle()
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "login_failed") is not None
    assert len(login_flows(rig.hass)) == 1


async def test_login_failure_when_idle_only_asks_to_sign_in(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rt.on_auth_failed()
    await rig.settle()
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "login_failed") is None
    assert len(login_flows(rig.hass)) == 1


async def test_login_issue_goes_when_reports_return(rig):
    rt, cook_id = await started(rig)
    rt.on_auth_failed()
    rt.on_state(report("cook", cook_id))
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "login_failed") is None


async def test_login_issue_from_before_a_reload_goes_with_the_first_report(rig):
    """Signing in again reloads the integration, so a new runtime must clear the old issue."""
    rt, cook_id = await started(rig)
    rt.on_auth_failed()
    await rt.shutdown()
    after = await rig.boot()
    after.on_state(report("cook", cook_id))
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, "login_failed") is None


# --- availability -----------------------------------------------------------


async def test_stale_60s_unavailable(rig, freezer):
    rt = await rig.boot()
    told = []
    rt.add_listener(lambda: told.append(rt.available))
    assert rt.available is False
    rt.on_state(report("idle"))
    assert rt.available is True
    await rig.advance(freezer, 59)
    assert rt.available is True
    await rig.advance(freezer, 6)
    assert rt.available is False
    assert told[-1] is False  # entities are told without a new report


async def test_disconnected_unavailable(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rt.on_status(False)
    assert rt.available is False


async def test_listener_removed_and_bad_listener_harmless(rig):
    rt = await rig.boot()
    told = []

    def broken():
        raise RuntimeError

    rt.add_listener(broken)
    remove = rt.add_listener(lambda: told.append(1))
    rt.on_state(report("idle"))
    remove()
    rt.on_state(report("idle"))
    assert told == [1]


# --- total hours ------------------------------------------------------------


async def test_total_hours_counts_cook_only(rig, freezer):
    rt = await rig.boot()
    for mode in ("idle", "idle"):
        rt.on_state(report(mode))
        freezer.tick(timedelta(seconds=30))
    assert rt.total_cook_hours == 0
    rt.on_state(report("cook", OTHER))
    for _ in range(4):
        freezer.tick(timedelta(seconds=45))
        rt.on_state(report("cook", OTHER))
    freezer.tick(timedelta(seconds=45))
    rt.on_state(report("idle"))
    freezer.tick(timedelta(seconds=45))
    rt.on_state(report("idle"))
    assert rt.total_cook_hours == pytest.approx(180 / 3600)


async def test_total_hours_skip_a_gap_in_reports(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    freezer.tick(timedelta(seconds=30))
    rt.on_state(report("cook", OTHER))
    freezer.tick(timedelta(minutes=30))
    rt.on_state(report("cook", OTHER))
    assert rt.total_cook_hours == pytest.approx(30 / 3600)


async def test_total_hours_skip_a_disconnect(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    rt.on_status(False)
    freezer.tick(timedelta(seconds=30))
    rt.on_status(True)
    rt.on_state(report("cook", OTHER))
    assert rt.total_cook_hours == 0


async def test_total_hours_survive_restart(rig, freezer):
    rig.seed(cook_id=None, stop_at=None, total_cook_hours=2.5)
    rt = await rig.boot()
    assert rt.total_cook_hours == 2.5
    rt.on_state(report("cook", OTHER))
    freezer.tick(timedelta(seconds=36))
    rt.on_state(report("cook", OTHER))
    await rt.shutdown()
    assert rig.saved["total_cook_hours"] == pytest.approx(2.51)
    after = await rig.boot()
    assert after.total_cook_hours == pytest.approx(2.51)


async def test_total_hours_not_written_on_every_report(rig, freezer, monkeypatch):
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    writes = []
    with monkeypatch.context() as patch:
        patch.setattr(rt._store, "async_delay_save", lambda *a: writes.append(a))
        patch.setattr(rt._store, "async_save", lambda *a: writes.append(a))
        for _ in range(120):
            freezer.tick(timedelta(seconds=1))
            rt.on_state(report("cook", OTHER))
    assert len(writes) == 1


# --- events -----------------------------------------------------------------


async def test_target_reached_fires_once(rig):
    events = async_capture_events(rig.hass, EVENT_TARGET_REACHED)
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id, stage_mode="entering", dry=40))
    await rig.settle()
    assert events == []
    rt.on_state(report("cook", cook_id, stage_mode="running", dry=90))
    rt.on_state(report("cook", cook_id, stage_mode="running", dry=95))
    rt.on_state(report("cook", cook_id, stage_mode="running", dry=120))
    await rig.settle()
    assert [e.data["cook_id"] for e in events] == [cook_id]


async def test_target_reached_when_setpoint_first_reached(rig):
    events = async_capture_events(rig.hass, EVENT_TARGET_REACHED)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rt.on_state(report("cook", OTHER, stage_mode="running", dry=99))
    await rig.settle()
    assert events == []
    rt.on_state(report("cook", OTHER, stage_mode="running", dry=100))
    await rig.settle()
    assert len(events) == 1


async def test_target_reached_once_per_cook_not_once_ever(rig):
    events = async_capture_events(rig.hass, EVENT_TARGET_REACHED)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rt.on_state(report("cook", "ios-a", dry=100))
    rt.on_state(report("idle"))
    rt.on_state(report("cook", "ios-b", dry=100))
    await rig.settle()
    assert [e.data["cook_id"] for e in events] == ["ios-a", "ios-b"]


async def test_target_reached_not_fired_by_the_hidden_end_stage(rig):
    events = async_capture_events(rig.hass, EVENT_TARGET_REACHED)
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    rt.on_state(report("cook", cook_id, stages=TIMED, index=1, dry=60))  # 25 C target, 60 C oven
    await rig.settle()
    assert events == []


async def test_target_not_fired_again_after_restart(rig):
    events = async_capture_events(rig.hass, EVENT_TARGET_REACHED)
    rt = await rig.boot()
    rt.on_state(report("idle"))
    rt.on_state(report("cook", OTHER, dry=100))
    await rig.settle()
    assert len(events) == 1
    await rt.shutdown()
    after = await rig.boot()
    after.on_state(report("cook", OTHER, dry=100))
    await rig.settle()
    assert len(events) == 1


async def test_cook_finished_fires_once_after_our_cook_ends(rig):
    events = async_capture_events(rig.hass, EVENT_COOK_FINISHED)
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id))
    await rig.settle()
    assert events == []
    rt.on_state(report("idle"))
    rt.on_state(report("idle"))
    await rig.settle()
    assert [e.data["cook_id"] for e in events] == [cook_id]


async def test_cook_finished_follows_the_stop_for_an_ended_timer(rig):
    events = async_capture_events(rig.hass, EVENT_COOK_FINISHED)
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    rt.on_state(report("cook", cook_id, stages=TIMED, index=1))
    await rig.settle()
    assert rig.conn.count(STOP) == 1
    assert events == []
    rt.on_state(report("idle"))
    await rig.settle()
    assert len(events) == 1


async def test_cook_finished_for_an_app_cook(rig):
    events = async_capture_events(rig.hass, EVENT_COOK_FINISHED)
    rt = await rig.boot()
    rt.on_state(report("cook", OTHER))
    rt.on_state(report("idle"))
    await rig.settle()
    assert [e.data["cook_id"] for e in events] == [OTHER]


# --- update -----------------------------------------------------------------


async def test_update_sends_changed_active_stage(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-this", stages=NO_TIMER))
    await rt.update({"fan": 50})
    command, payload = rig.conn.sent[-1]
    assert command == UPDATE
    assert payload["do"]["fan"]["speed"] == 50
    assert payload["id"] == NO_TIMER[0]["id"]


async def test_update_with_no_cook_is_a_plain_error(rig):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    with pytest.raises(HomeAssistantError):
        await rt.update({"fan": 50})
    assert rig.conn.sent == []


async def test_update_on_a_stale_report_is_refused(rig, freezer):
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-this"))
    freezer.tick(timedelta(seconds=61))
    with pytest.raises(HomeAssistantError):
        await rt.update({"fan": 50})
    assert rig.conn.sent == []


async def test_update_refused_in_hidden_end_stage(rig):
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-this", stages=TIMED, index=1))
    with pytest.raises(HomeAssistantError):
        await rt.update({"fan": 50})
    assert rig.conn.sent == []


async def test_update_timer_moves_stop_time_of_running_timer(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    began = dt_util.utcnow()
    await rig.advance(freezer, 600)  # 10 minutes in
    rt.on_state(report("cook", cook_id, stages=TIMED, timer="running", timer_started=began))
    await rt.update({"timer_seconds": 7200})
    await rig.settle()
    # 110 minutes left, plus the 10 minute margin
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=120)


async def test_update_timer_can_bring_stop_time_forward(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    rt.on_state(report("cook", cook_id, stages=TIMED, timer="running", timer_started=dt_util.utcnow()))
    await rt.update({"timer_seconds": 300})
    await rig.settle()
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=15)


async def test_update_timer_not_started_adds_the_preheat_allowance(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30, timer_mode="Manually"))
    rt.on_state(report("cook", cook_id, stages=TIMED, timer="idle"))
    await rt.update({"timer_seconds": 3600})
    await rig.settle()
    expected = dt_util.utcnow() + timedelta(minutes=60 + 10 + 60)
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == expected


async def test_start_timer_moves_stop_time_with_no_preheat_allowance(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30, timer_mode="Manually"))
    rt.on_state(report("cook", cook_id, stages=TIMED, timer="idle"))
    await rt.update({"start_timer": True})
    await rig.settle()
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=40)


async def test_own_max_minutes_stands_after_timer_change_and_restart(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30, max_minutes=45))
    stop_at = rig.saved["stop_at"]
    assert rig.saved["max_minutes"] == 45
    await rt.shutdown()
    after = await rig.boot()
    after.on_state(report("cook", cook_id, stages=TIMED, timer="running", timer_started=dt_util.utcnow()))
    await after.update({"timer_seconds": 7200})
    await rig.settle()
    assert rig.saved["stop_at"] == stop_at


async def test_default_limit_never_extends_without_a_timer(rig, freezer):
    rt, cook_id = await started(rig)
    rt.on_state(report("cook", cook_id, stages=NO_TIMER))
    stop_at = rig.saved["stop_at"]
    await rt.update({"start_timer": True})
    await rig.settle()
    assert rig.saved["stop_at"] == stop_at


async def test_app_cook_timer_change_touches_no_stop_time(rig, freezer):
    rt, cook_id = await started(rig, shown=False)
    stop_at = rig.saved["stop_at"]
    rt.on_state(report("cook", OTHER, stages=TIMED, timer="running", timer_started=dt_util.utcnow()))
    await rt.update({"timer_seconds": 7200})
    await rig.settle()
    assert rig.saved["stop_at"] == stop_at


@pytest.mark.parametrize("maximum, minutes", [(None, 180), (45, 45)])
async def test_custom_cook_limit_stands_after_a_timer_change(rig, freezer, maximum, minutes):
    rt = await rig.boot()
    rt.on_state(report("idle"))
    cook_id = await rt.start_custom(ONE_STAGE, maximum)
    await rig.settle()
    stop_at = rig.saved["stop_at"]
    assert rig.saved["max_minutes"] == minutes
    rt.on_state(report("cook", cook_id, stages=ONE_STAGE, timer="running", timer_started=dt_util.utcnow()))
    await rt.update({"timer_seconds": 36000})
    await rig.settle()
    assert rig.saved["stop_at"] == stop_at


async def test_timer_change_does_not_cut_a_custom_cook_of_several_stages(rig, freezer):
    two = [dict(ONE_STAGE[0], title="a"), dict(ONE_STAGE[0], title="b")]
    rt = await rig.boot()
    rt.on_state(report("idle"))
    cook_id = await rt.start_custom(two, None)
    await rig.settle()
    stop_at = rig.saved["stop_at"]
    rt.on_state(report("cook", cook_id, stages=two, timer="running", timer_started=dt_util.utcnow()))
    await rt.update({"timer_seconds": 60})
    await rig.settle()
    assert rig.saved["stop_at"] == stop_at


async def test_timer_change_refused_moves_nothing(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    stop_at = rig.saved["stop_at"]
    rt.on_state(report("cook", cook_id, stages=TIMED, timer="running", timer_started=dt_util.utcnow()))
    rig.conn.errors = [OvenRefused("no")]
    with pytest.raises(HomeAssistantError):
        await rt.update({"timer_seconds": 7200})
    assert rig.saved["stop_at"] == stop_at


async def test_timer_change_does_not_put_off_a_due_stop(rig, freezer):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=1, max_minutes=None))
    rt.on_state(report("cook", cook_id, stages=TIMED, timer="running", timer_started=dt_util.utcnow()))
    rt._stop_at = dt_util.utcnow()
    await rt.update({"timer_seconds": 7200})
    await rig.settle()
    assert rt._stop_at <= dt_util.utcnow()


@pytest.mark.parametrize("error", [OvenRefused("no"), OvenNoReply("x"), OvenOffline("x")])
async def test_update_failure_is_a_plain_error(rig, error):
    rt = await rig.boot()
    rt.on_state(report("cook", "ios-this"))
    rig.conn.errors = [error]
    with pytest.raises(HomeAssistantError):
        await rt.update({"fan": 50})
