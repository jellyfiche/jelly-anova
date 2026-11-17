"""Cook status, the Continue button, and update_cook's minutes_left.

PREHEAT_ENTRY, MANUAL_ENTRY, TIMER_EXIT and the timer nodes are the oven's own shapes from the
battery and app-cook captures (preheat_wait, manual_go, appcook-timed, direct_cook). FOOD_ENTRY,
USER_EXIT, EMPTY_EXIT and the probe entry are built from the published stage schema: no capture
shows them, so the statuses they give are untested on the oven.
"""

import copy
from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.jelly_anova import status
from custom_components.jelly_anova.const import END_STAGE_TITLE
from custom_components.jelly_anova.cook import CookRequest
from custom_components.jelly_anova.state import parse_state

from .test_runtime import rig, started  # noqa: F401 - rig is a fixture
from .test_services import oven  # noqa: F401 - oven is a fixture
from .test_setup import fake  # noqa: F401 - fake is a fixture

PREHEAT_ENTRY = {"conditions": {"or": {"nodes.temperatureBulbs.dry.current.celsius": {">=": 60}}}}
MANUAL_ENTRY = {"conditions": {"or": {"userAction": {"=": True}}}}
FOOD_ENTRY = {"conditions": {"or": {"userAction": {"=": True}, "nodes.cavityCamera.isEmpty": {"=": False}}}}
TIMER_EXIT = {"conditions": {"and": {"nodes.timer.mode": {"=": "completed"}}}}
USER_EXIT = {"conditions": {"and": {"nodes.timer.mode": {"=": "completed"}, "userAction": {"=": True}}}}
EMPTY_EXIT = {"conditions": {"and": {"nodes.cavityCamera.isEmpty": {"=": True}}}}


def stage(stage_id="s1", seconds=300, timer_entry=None, entry=None, exit=TIMER_EXIT, title="", probe=None):
    do = {"temperatureBulbs": {"mode": "dry", "dry": {"setpoint": {"celsius": 60}}}}
    if seconds is not None:
        do["timer"] = {"initial": seconds}
        if timer_entry is not None:
            do["timer"]["entry"] = timer_entry
    if probe is not None:
        do["temperatureProbe"] = {"setpoint": {"celsius": probe}}
    made = {"id": stage_id, "title": title, "do": do}
    if entry is not None:
        made["entry"] = entry
    if exit is not None:
        made["exit"] = exit
    return made


def oven_report(mode="cook", stages=None, index=0, stage_mode="running", timer=None, probe=None, at=None, cook_id="ios-x", probe_target=None):
    """A report; `timer` is the timer node as the oven sends it, `probe` the probe node."""
    nodes = {"temperatureBulbs": {"mode": "dry", "dry": {"current": {"celsius": 50}}}}
    if timer is not None:
        nodes["timer"] = timer
    if probe is not None:
        nodes["temperatureProbe"] = dict(probe)
        if probe_target is not None:
            nodes["temperatureProbe"]["setpoint"] = {"celsius": probe_target}
    raw = {"updatedTimestamp": (at or dt_util.utcnow()).isoformat(), "state": {"mode": mode}, "nodes": nodes}
    if mode == "cook":
        raw["cook"] = {
            "cookId": cook_id,
            "originSource": "ios",
            "activeStageIndex": index,
            "activeStageMode": stage_mode,
            "stages": stages if stages is not None else [stage()],
        }
    return parse_state({"state": raw}, None)


WAITING = {"initial": 300, "mode": "idle"}
RUNNING = {"initial": 300, "mode": "running", "startedAtTimestamp": "2026-11-01T17:07:36Z"}
ENDED = {"mode": "idle"}


# --- status, from report shapes ---------------------------------------------------


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (lambda: oven_report("idle"), status.IDLE),
        (lambda: oven_report("descale"), status.DESCALING),
        # The app's preheat: the stage's entry, reported as "entering" (appcook-timed)
        (lambda: oven_report(stages=[stage(entry=PREHEAT_ENTRY)], stage_mode="entering", timer=RUNNING), status.PREHEATING),
        # Our When Preheated: the timer waits on the bulb (preheat_wait)
        (lambda: oven_report(stages=[stage(timer_entry=PREHEAT_ENTRY)], timer=WAITING), status.PREHEATING),
        # Our Manually: the timer waits on you (manual_go)
        (lambda: oven_report(stages=[stage(timer_entry=MANUAL_ENTRY)], timer=WAITING), status.START_TIMER),
        (lambda: oven_report(stages=[stage(timer_entry=FOOD_ENTRY)], timer=WAITING), status.ADD_FOOD),
        (lambda: oven_report(timer=RUNNING), status.COOKING),
        (lambda: oven_report(stages=[stage(seconds=None, exit=None)]), status.COOKING),
        # The timer ran out: idle without initial (appcook-timed, preheat_wait), or completed
        (lambda: oven_report(timer=ENDED), status.TIMER_DONE),
        (lambda: oven_report(timer={"mode": "completed"}), status.TIMER_DONE),
        (lambda: oven_report(stages=[stage(exit=USER_EXIT), stage("s2")], timer=ENDED), status.START_NEXT_STAGE),
        (lambda: oven_report(stages=[stage(exit=EMPTY_EXIT)], timer=ENDED), status.REMOVE_FOOD),
        (lambda: oven_report(stages=[stage(), stage("s2", entry=MANUAL_ENTRY)], index=1, stage_mode="entering"), status.START_NEXT_STAGE),
        (
            lambda: oven_report(
                stages=[stage(entry={"conditions": {"and": {"nodes.temperatureProbe.connected": {"=": True}}}})],
                stage_mode="entering",
                probe={"connected": False},
            ),
            status.PLUG_IN_PROBE,
        ),
        # A probe target beside a running timer, probe out (direct_cook): still cooking
        (
            lambda: oven_report(stages=[stage(probe=50)], timer=RUNNING, probe={"connected": False}, probe_target=50),
            status.COOKING,
        ),
        # A stage that ends on the probe, probe out
        (
            lambda: oven_report(
                stages=[stage(seconds=None, exit={"conditions": {"and": {"nodes.temperatureProbe.current.celsius": {">=": 50}}}})],
                probe={"connected": False},
            ),
            status.PLUG_IN_PROBE,
        ),
        # A last stage that waits on you, with nothing after it
        (lambda: oven_report(stages=[stage(exit=USER_EXIT)], timer=ENDED), status.TIMER_DONE),
        (lambda: oven_report(stages=[stage(exit=USER_EXIT), stage("end", title=END_STAGE_TITLE)], timer=ENDED), status.TIMER_DONE),
        # A timer that starts when preheated or when you press, still preheating
        (
            lambda: oven_report(
                stages=[stage(timer_entry={"conditions": {"or": {"userAction": {"=": True}, "nodes.temperatureBulbs.dry.current.celsius": {">=": 60}}}})],
                timer=WAITING,
            ),
            status.PREHEATING,
        ),
        # The hidden last stage of a Home Assistant cook
        (lambda: oven_report(stages=[stage(), stage("end", title=END_STAGE_TITLE)], index=1, timer=RUNNING), status.FINISHED),
    ],
)
def test_status_from_reports(report, expected):
    assert status.cook_status(report()) == expected


def test_status_unknown_without_a_report_or_cook():
    assert status.cook_status(None) is None
    assert status.cook_status(oven_report("something-new")) is None


def test_every_status_has_wording():
    import json
    from pathlib import Path

    package = Path(__file__).parents[1] / "custom_components" / "jelly_anova"
    for name in ("strings.json", "translations/en.json"):
        words = json.loads((package / name).read_text())["entity"]["sensor"]["cook_status"]["state"]
        assert set(words) == set(status.OPTIONS), name


def test_condition_paths_reads_nested_groups():
    nested = {"and": {"a": {"=": 1}, "or": {"b": {"=": 2}, "not": {"c": {"=": 3}}}}}
    assert status.condition_paths(nested) == {"a", "b", "c"}
    assert status.condition_paths(None) == set()


# --- Continue, in the runtime ------------------------------------------------------


async def test_continue_releases_a_timer_waiting_for_you(rig):
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30, timer_mode="Manually"))
    stages = rig.conn.sent[-1][1]["stages"]
    rt.on_state(oven_report(stages=stages, timer={"initial": 1800, "mode": "idle"}, cook_id=cook_id))
    rig.conn.sent.clear()
    await rt.continue_cook()
    (command, payload), = rig.conn.sent
    assert command == "CMD_APO_UPDATE_COOK_STAGE"
    assert "entry" not in payload["do"]["timer"] and payload["do"]["timer"]["initial"] == 1800


async def test_continue_starts_the_next_stage_when_a_stage_waits_for_you(rig):
    rt = await rig.boot()
    stages = [stage("s1", exit=USER_EXIT), stage("s2")]
    rt.on_state(oven_report(stages=stages, timer=ENDED))
    await rt.continue_cook()
    assert rig.conn.sent == [("CMD_APO_START_STAGE", {"stageId": "s2"})]


async def test_continue_refused_when_nothing_waits(rig):
    rt = await rig.boot()
    rt.on_state(oven_report(timer=RUNNING))
    with pytest.raises(ServiceValidationError, match="not waiting"):
        await rt.continue_cook()
    assert rig.conn.sent == []


async def test_continue_refused_with_no_cook(rig):
    rt = await rig.boot()
    rt.on_state(oven_report("idle"))
    with pytest.raises(HomeAssistantError, match="No cook is running"):
        await rt.continue_cook()
    assert rig.conn.sent == []


async def test_continue_with_no_stage_after_is_refused(rig):
    rt = await rig.boot()
    rt.on_state(oven_report(stages=[stage(exit=USER_EXIT)], timer=ENDED))
    with pytest.raises(ServiceValidationError, match="not waiting"):
        await rt.continue_cook()
    assert rig.conn.sent == []


async def test_continue_starts_a_stage_waiting_to_begin_by_its_own_id(rig):
    rt = await rig.boot()
    stages = [stage("s1"), stage("s2", entry=MANUAL_ENTRY), stage("s3")]
    rt.on_state(oven_report(stages=stages, index=1, stage_mode="entering"))
    await rt.continue_cook()
    assert rig.conn.sent == [("CMD_APO_START_STAGE", {"stageId": "s2"})]


async def test_continue_pressed_twice_starts_the_stage_once(rig, freezer):
    rt = await rig.boot()
    stages = [stage("s1", exit=USER_EXIT), stage("s2")]
    rt.on_state(oven_report(stages=stages, timer=ENDED))
    await rt.continue_cook()
    await rt.continue_cook()
    assert rig.conn.count("CMD_APO_START_STAGE") == 1
    freezer.tick(timedelta(seconds=6))
    rt.on_state(oven_report(stages=stages, timer=ENDED))
    await rt.continue_cook()
    assert rig.conn.count("CMD_APO_START_STAGE") == 2


async def test_continue_releases_a_timer_you_may_start_before_preheat_ends(rig):
    rt = await rig.boot()
    entry = {"conditions": {"or": {"userAction": {"=": True}, "nodes.temperatureBulbs.dry.current.celsius": {">=": 60}}}}
    rt.on_state(oven_report(stages=[stage(timer_entry=entry)], timer=WAITING))
    await rt.continue_cook()
    (command, payload), = rig.conn.sent
    assert command == "CMD_APO_UPDATE_COOK_STAGE" and "entry" not in payload["do"]["timer"]


async def test_continue_refused_while_preheating_on_temperature_alone(rig):
    rt = await rig.boot()
    rt.on_state(oven_report(stages=[stage(timer_entry=PREHEAT_ENTRY)], timer=WAITING))
    with pytest.raises(ServiceValidationError, match="not waiting"):
        await rt.continue_cook()
    assert rig.conn.sent == []


def test_next_stage_skips_nothing_and_ignores_the_hidden_end():
    report = oven_report(stages=[stage("a"), stage("b"), stage("end", title=END_STAGE_TITLE)], index=1)
    assert status.next_stage_id(report.cook) is None
    report = oven_report(stages=[stage("a"), stage("b")], index=0)
    assert status.next_stage_id(report.cook) == "b"


# --- minutes_left ----------------------------------------------------------------


async def test_minutes_left_adds_the_time_already_run(rig, freezer):
    """Observation 0365's case: 8 minutes in, 'stop in 15 minutes' is a 23-minute timer."""
    rt, cook_id = await started(rig, CookRequest(temperature_f=350, minutes=30))
    stages = copy.deepcopy(rig.conn.sent[-1][1]["stages"])
    began = dt_util.utcnow()
    freezer.tick(timedelta(minutes=8, seconds=30))
    timer = {"initial": 1800, "mode": "running", "startedAtTimestamp": began.isoformat()}
    rt.on_state(oven_report(stages=stages, timer=timer, cook_id=cook_id))
    rig.conn.sent.clear()
    await rt.update({"minutes_left": 15})
    await rig.settle()
    (command, payload), = rig.conn.sent
    assert command == "CMD_APO_UPDATE_COOK_STAGE"
    assert payload["do"]["timer"]["initial"] == 510 + 900
    # The stop time follows: 15 minutes left plus the 10-minute margin
    assert dt_util.parse_datetime(rig.saved["stop_at"]) == dt_util.utcnow() + timedelta(minutes=25)


@pytest.mark.parametrize(
    ("timer", "stages", "message"),
    [
        (WAITING, [stage(timer_entry=MANUAL_ENTRY)], "not started yet"),
        (ENDED, [stage()], "not running"),
        (None, [stage(seconds=None)], "no timer"),
    ],
)
async def test_minutes_left_refused_unless_a_timer_runs(rig, timer, stages, message):
    rt = await rig.boot()
    rt.on_state(oven_report(stages=stages, timer=timer))
    with pytest.raises(ServiceValidationError, match=message):
        await rt.update({"minutes_left": 15})
    assert rig.conn.sent == []


@pytest.mark.parametrize(("minutes", "message"), [(0, "at least 1"), (-5, "at least 1"), (float("inf"), "at least 1"), (True, "at least 1"), (1441, "at most 1440")])
async def test_minutes_left_must_be_a_real_number_of_minutes(rig, minutes, message):
    rt = await rig.boot()
    rt.on_state(oven_report(timer=RUNNING))
    with pytest.raises(ServiceValidationError, match=message):
        await rt.update({"minutes_left": minutes})
    assert rig.conn.sent == []


# --- through Home Assistant ---------------------------------------------------------


async def test_cook_status_sensor_reads_as_a_phrase(oven):
    oven.feed(oven_report(stages=[stage(timer_entry=MANUAL_ENTRY)], timer=WAITING))
    await oven.hass.async_block_till_done()
    assert oven.hass.states.get(oven.entity("sensor", "cook_status")).state == status.START_TIMER


async def test_continue_button_sends_the_release(oven):
    oven.feed(oven_report(stages=[stage(timer_entry=MANUAL_ENTRY)], timer=WAITING))
    await oven.hass.async_block_till_done()
    await oven.hass.services.async_call(
        "button", "press", {"entity_id": oven.entity("button", "continue_cook")}, blocking=True
    )
    assert oven.commands() == ["CMD_APO_UPDATE_COOK_STAGE"]


async def test_continue_button_when_nothing_waits_is_a_plain_error(oven):
    oven.feed(oven_report(timer=RUNNING))
    await oven.hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="not waiting"):
        await oven.hass.services.async_call(
            "button", "press", {"entity_id": oven.entity("button", "continue_cook")}, blocking=True
        )
    assert oven.commands() == []


async def test_update_cook_minutes_left_and_timer_together_refused(oven):
    oven.feed(oven_report(timer=RUNNING))
    await oven.hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="not both"):
        await oven.call("update_cook", {"timer": {"minutes": 20}, "minutes_left": 15})
    assert oven.commands() == []


async def test_update_cook_minutes_left_through_the_action(oven, freezer):
    began = dt_util.utcnow()
    freezer.tick(timedelta(minutes=8))
    timer = {"initial": 1800, "mode": "running", "startedAtTimestamp": began.isoformat()}
    oven.feed(oven_report(timer=timer))
    await oven.hass.async_block_till_done()
    await oven.call("update_cook", {"minutes_left": 15})
    assert oven.payload("CMD_APO_UPDATE_COOK_STAGE")["do"]["timer"]["initial"] == 480 + 900
