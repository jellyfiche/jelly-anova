"""Tests for report parsing. No Home Assistant needed."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.jelly_anova.const import END_STAGE_TITLE
from custom_components.jelly_anova.state import OvenState, is_newer, parse_state

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    """Return the payload of a fixture report."""
    return copy.deepcopy(json.loads((FIXTURES / name).read_text())["payload"])


def at(payload: dict, stamp: str) -> dict:
    """Return payload with a new report time."""
    payload["state"]["updatedTimestamp"] = stamp
    return payload


def test_idle_report():
    s = parse_state(load("idle.json"), None)
    assert s.mode == "idle"
    assert s.updated == datetime(2026, 11, 1, 17, 27, 50, 293000, tzinfo=UTC)
    assert s.dry_c == 46.48
    assert s.wet_c == 44.76
    assert s.dry_top_c == 49.75
    assert s.dry_bottom_c == 45.79
    assert s.probe_connected is False
    assert s.probe_c is None
    assert s.door_closed is True
    assert s.vent_closed is True
    assert s.water_low is False
    assert s.water_empty is False
    assert s.water_removed is False
    assert s.waste_full is False
    assert s.waste_removed is False
    assert s.lamp_on is False
    assert s.oven_empty is True
    assert s.descale_needed is False
    assert s.any_failed is False
    assert s.usage_hours["rear"] == 35.3
    assert s.fan_speed == "max"
    assert s.fan_speeds == {
        "exhaust": "off",
        "led": "mid",
        "power_board": "off",
        "display": "mid",
    }
    assert s.boiler_c == 38.95
    assert s.evaporator_c == 44.32
    assert s.firmware == "11_235_1.4.7_01.01.26"
    assert s.firmware_updated == datetime(2026, 10, 30, 22, 40, 18, tzinfo=UTC)
    assert s.online is True
    assert s.vent_state == "closed"
    assert s.door_lamp_preference == "on"
    assert s.cavity_overheated is False
    assert s.triacs_failed is False
    assert s.display_board_c == 41
    assert s.dc12v_faults == 0
    assert s.hardware == "nxpke1-i500"
    assert s.mains_volts == 120
    assert s.mains_hertz == 60
    assert s.last_disconnected == datetime(2026, 10, 30, 22, 46, 45, tzinfo=UTC)
    assert s.food == []
    assert s.cook is None


def test_login_cook_report():
    s = parse_state(load("login_cook.json"), None)
    assert s.mode == "cook"
    assert s.cook is not None
    assert s.cook.origin == "ios"
    assert s.cook.cook_id.startswith("ios-")
    assert s.cook.stage_index == 0
    assert s.cook.stage_count == 1
    assert s.cook.stage_mode == "running"
    assert s.cook.started == datetime(2026, 11, 1, 16, 53, 47, tzinfo=UTC)
    assert s.cook.timer_mode == "running"
    assert s.cook.timer_initial == 60
    assert s.cook.rack == 3
    assert s.cook.sous_vide is False
    assert s.cook.in_end_stage is False
    assert s.cook.active_stage["id"] == s.cook.stages[0]["id"]
    assert s.target_c == 60
    assert s.steam_mode == "steam-percentage"
    assert s.steam_percent == 40
    assert s.target_steam_percent == 30
    assert s.watts["evaporator"] == 220


def test_missing_nodes_give_none():
    payload = {"state": {"nodes": {}, "state": {}, "updatedTimestamp": "2026-11-01T17:27:50.293Z"}}
    s = parse_state(payload, None)
    assert isinstance(s, OvenState)
    for name in (
        "mode dry_c wet_c dry_top_c dry_bottom_c target_c probe_c target_probe_c "
        "probe_connected door_closed vent_closed water_low water_empty water_removed "
        "waste_full waste_removed lamp_on oven_empty descale_needed any_failed "
        "steam_mode humidity target_humidity fan_speed boiler_c evaporator_c firmware "
        "firmware_updated online vent_state steam_percent target_steam_percent "
        "door_lamp_preference cavity_overheated triacs_failed display_board_c "
        "dc12v_faults hardware mains_volts mains_hertz last_disconnected cook"
    ).split():
        assert getattr(s, name) is None, name
    assert s.watts == {}
    assert s.usage_hours == {}
    assert s.fan_speeds == {}
    assert s.food == []


def test_empty_payload_does_not_raise():
    s = parse_state({}, None)
    assert s.cook is None
    assert s.dry_c is None


def test_unknown_field_ignored():
    payload = load("idle.json")
    payload["state"]["nodes"]["flux"] = {"capacitor": 1}
    payload["state"]["nodes"]["door"]["wobble"] = True
    payload["state"]["brandNew"] = [1, 2]
    s = parse_state(payload, None)
    assert s.door_closed is True
    assert s == parse_state(load("idle.json"), None)


def test_setpoint_while_idle_is_not_heating():
    s = parse_state(load("idle.json"), None)
    # The rear element reads on=true at 0 W while idle; the setpoint stays in the report.
    assert s.target_c == 60
    assert s.watts == {
        "top": 0,
        "bottom": 0,
        "rear": 0,
        "boiler": 0,
        "evaporator": 0,
    }
    assert all(w == 0 for w in s.watts.values())


def test_stop_report_is_idle_without_cook():
    s = parse_state(load("stop.json"), None)
    assert s.mode == "idle"
    assert s.cook is None
    assert all(w == 0 for w in s.watts.values())


def test_detection_list_becomes_food():
    payload = load("idle.json")
    hit = [{"type": "food", "categoryId": 202, "confidence": 0.965}]
    payload["state"]["nodes"]["cavityCamera"]["detection"] = hit
    assert parse_state(payload, None).food == hit
    payload["state"]["nodes"]["cavityCamera"]["detection"] = ""
    assert parse_state(payload, None).food == []


def test_door_lamp_is_ignored_for_lamp_on():
    payload = load("idle.json")
    payload["state"]["nodes"]["doorLamp"]["on"] = True
    assert parse_state(payload, None).lamp_on is False
    payload["state"]["nodes"]["cavityLamp"]["on"] = True
    assert parse_state(payload, None).lamp_on is True


def test_failed_element_sets_any_failed():
    payload = load("idle.json")
    payload["state"]["nodes"]["heatingElements"]["top"]["failed"] = True
    assert parse_state(payload, None).any_failed is True


def _glitch_pair():
    first = parse_state(at(load("idle.json"), "2026-11-01T17:00:00.000Z"), None)
    bad = at(load("idle.json"), "2026-11-01T17:00:02.000Z")
    bad["state"]["nodes"]["temperatureBulbs"]["dry"]["current"]["celsius"] = 0
    return first, bad


def test_zero_glitch_keeps_previous():
    first = parse_state(at(load("login_cook.json"), "2026-11-01T17:00:00.000Z"), None)
    assert first.dry_c == 54.8
    bad = at(load("login_cook.json"), "2026-11-01T17:00:02.000Z")
    bad["state"]["nodes"]["temperatureBulbs"]["dry"]["current"]["celsius"] = 0
    held = parse_state(bad, first)
    assert held.dry_c == 54.8
    assert held.updated == datetime(2026, 11, 1, 17, 0, 2, tzinfo=UTC)
    # Still zero 4 s after the last good reading: still held.
    bad2 = at(copy.deepcopy(bad), "2026-11-01T17:00:04.900Z")
    assert parse_state(bad2, held).dry_c == 54.8
    # Past 5 s of zeros: accepted as real.
    bad3 = at(copy.deepcopy(bad), "2026-11-01T17:00:05.500Z")
    assert parse_state(bad3, parse_state(bad2, held)).dry_c == 0


def test_zero_glitch_with_no_previous_is_accepted():
    _, bad = _glitch_pair()
    assert parse_state(bad, None).dry_c == 0


def test_real_low_reading_accepted():
    first = at(load("idle.json"), "2026-11-01T17:00:00.000Z")
    first["state"]["nodes"]["temperatureBulbs"]["dry"]["current"]["celsius"] = 1.0
    prev = parse_state(first, None)
    low = at(load("idle.json"), "2026-11-01T17:00:01.000Z")
    low["state"]["nodes"]["temperatureBulbs"]["dry"]["current"]["celsius"] = 0.0
    assert parse_state(low, prev).dry_c == 0.0


def test_good_reading_after_glitch_resets_hold():
    first, bad = _glitch_pair()
    held = parse_state(bad, first)
    back = at(load("idle.json"), "2026-11-01T17:00:03.000Z")
    assert parse_state(back, held).dry_c == 46.48


def _with_end_stage(payload: dict, active_index: int) -> dict:
    cook = payload["state"]["cook"]
    end = copy.deepcopy(cook["stages"][0])
    end["id"] = "end-stage-id"
    end["title"] = END_STAGE_TITLE
    end["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] = 25
    cook["stages"].append(end)
    cook["activeStageIndex"] = active_index
    cook["activeStageId"] = cook["stages"][active_index]["id"]
    return payload


def test_hidden_end_stage_left_out():
    s = parse_state(_with_end_stage(load("login_cook.json"), 0), None)
    assert s.cook.stage_count == 1
    assert len(s.cook.stages) == 1
    assert all(st["title"] != END_STAGE_TITLE for st in s.cook.stages)
    assert s.cook.in_end_stage is False
    assert s.cook.stage_index == 0


def test_in_end_stage_flag():
    s = parse_state(_with_end_stage(load("login_cook.json"), 1), None)
    assert s.cook.in_end_stage is True
    assert s.cook.stage_count == 1
    assert len(s.cook.stages) == 1
    assert s.cook.active_stage["title"] == END_STAGE_TITLE
    assert s.cook.stage_index == 1


def test_sous_vide_no_timer():
    s = parse_state(load("app_sous_vide.json"), None)
    assert s.mode == "cook"
    assert s.cook.origin == "ios"
    assert s.cook.stage_mode == "entering"
    assert s.cook.timer_mode is None
    assert s.cook.timer_initial is None
    assert s.cook.sous_vide is True
    assert s.cook.stage_count == 1
    assert s.target_c == 55.9
    assert s.target_humidity == 100
    assert s.steam_mode == "relative-humidity"


def test_probe_connected_reads_temperature():
    payload = load("idle.json")
    probe = payload["state"]["nodes"]["temperatureProbe"]
    probe["connected"] = True
    probe["current"]["celsius"] = 48.5
    probe["setpoint"] = {"celsius": 60}
    s = parse_state(payload, None)
    assert s.probe_c == 48.5
    assert s.target_probe_c == 60


def test_is_newer():
    s = parse_state(load("idle.json"), None)
    assert is_newer(s, s.updated - timedelta(seconds=1)) is True
    assert is_newer(s, s.updated) is False
    assert is_newer(s, s.updated + timedelta(seconds=1)) is False


def test_state_is_frozen():
    s = parse_state(load("idle.json"), None)
    with pytest.raises(Exception):
        s.mode = "cook"  # type: ignore[misc]


def test_state_module_has_no_home_assistant_import():
    src = (Path(__file__).parent.parent / "custom_components/jelly_anova/state.py").read_text()
    assert "homeassistant" not in src


def test_timer_started_read_from_the_report():
    payload = load("login_cook.json")
    payload["state"]["nodes"]["timer"]["startedAtTimestamp"] = "2026-11-01T12:00:00Z"
    payload["state"]["cook"]["stages"][0].setdefault("do", {})["timer"] = {"initial": 60}
    payload["state"]["cook"]["activeStageIndex"] = 0
    cook = parse_state(payload, None).cook
    assert cook.timer_started == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def test_fault_details_are_kept():
    payload = load("idle.json")
    payload["state"]["nodes"]["heatingElements"]["top"]["failed"] = True
    payload["state"]["nodes"]["cavityLamp"]["failed"] = False
    payload["state"]["nodes"]["steamGenerators"]["boiler"]["numberOfOverCurrentInletPump"] = 3
    s = parse_state(payload, None)
    assert s.failed == {
        "top": True,
        "bottom": False,
        "rear": False,
        "boiler": False,
        "evaporator": False,
        "fan": False,
    }
    assert s.lamp_failed is False
    assert s.boiler_pump_faults == 3
    assert s.boiler_valve_faults == 0


def test_fault_details_missing_stay_none():
    s = parse_state({}, None)
    assert s.failed == {}
    assert s.lamp_failed is None
    assert s.boiler_pump_faults is None


def test_timer_waiting_only_when_node_is_idle_with_initial():
    """Real node shapes: waiting {idle, initial}; ended {idle}; running has initial and a start."""
    for node, waiting in (
        ({"mode": "idle", "initial": 60}, True),
        ({"mode": "idle"}, False),
        ({"mode": "running", "initial": 60, "startedAtTimestamp": "2026-11-01T16:42:52Z"}, False),
    ):
        payload = load("login_cook.json")
        payload["state"]["nodes"]["timer"] = node
        cook = parse_state(payload, None).cook
        assert cook.timer_waiting is waiting, node
        assert cook.timer_initial == 60  # always the stage's own setting
    assert parse_state(load("idle.json"), None).cook is None
