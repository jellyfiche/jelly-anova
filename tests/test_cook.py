"""Tests for the cook builder (pure Python)."""
import copy

import pytest

from custom_components.jelly_anova.const import END_STAGE_TITLE
from custom_components.jelly_anova.cook import (
    CookError,
    CookRequest,
    build_start,
    limit_minutes,
    updated_stage,
)


def build(**kw):
    payload, cook_id = build_start(CookRequest(**kw), "COOKER")
    return payload, cook_id


def first_stage(**kw):
    return build(**kw)[0]["stages"][0]


def test_matches_app_shape():
    payload, cook_id = build(temperature_c=60, minutes=1)
    assert cook_id.startswith("ios-") and payload["cookId"] == cook_id
    cook, end = payload["stages"]
    assert cook["id"].startswith("ios-") and cook["id"] != end["id"]
    assert cook == {
        "id": cook["id"],
        "title": "",
        "description": "",
        "rackPosition": 3,
        "do": {
            "type": "cook",
            "fan": {"speed": 100},
            "heatingElements": {"top": {"on": False}, "bottom": {"on": False}, "rear": {"on": True}},
            "exhaustVent": {"state": "closed"},
            "timer": {"initial": 60},
            "steamGenerators": {"mode": "steam-percentage", "steamPercentage": {"setpoint": 0}},
            "temperatureBulbs": {"mode": "dry", "dry": {"setpoint": {"celsius": 60}}},
        },
        "exit": {"conditions": {"and": {"nodes.timer.mode": {"=": "completed"}}}},
    }
    assert {k: v for k, v in payload.items() if k != "stages" and k != "cookId"} == {
        "cookerId": "COOKER", "cookableId": "", "title": "", "type": "oven_v2",
        "originSource": "ios", "cookableType": "manual", "rackPosition": 3,
    }


def test_timed_cook_ends_in_cool_stage():
    stages = build(temperature_c=60, minutes=5)[0]["stages"]
    assert len(stages) == 2
    end = stages[1]
    assert end["title"] == END_STAGE_TITLE
    do = end["do"]
    assert do["temperatureBulbs"] == {"mode": "dry", "dry": {"setpoint": {"celsius": 25}}}
    assert do["heatingElements"] == {"top": {"on": False}, "bottom": {"on": False}, "rear": {"on": True}}
    assert do["steamGenerators"] == {"mode": "steam-percentage", "steamPercentage": {"setpoint": 0}}
    assert do["timer"] == {"initial": 43200}


@pytest.fixture
def probe_proven(monkeypatch):
    monkeypatch.setattr("custom_components.jelly_anova.cook.PROBE_STAGE_PROVEN", True)


def test_probe_refused_until_proven():
    from custom_components.jelly_anova import cook
    assert cook.PROBE_STAGE_PROVEN is False
    with pytest.raises(CookError, match="not tested"):
        build(temperature_c=60, probe_c=50)
    with pytest.raises(CookError, match="not tested"):
        build(temperature_c=60, probe_f=122)


def test_untimed_cook_has_one_stage(probe_proven):
    stages = build(temperature_c=60, probe_c=50)[0]["stages"]
    assert len(stages) == 1
    assert stages[0]["do"]["temperatureProbe"] == {"setpoint": {"celsius": 50}}
    assert "timer" not in stages[0]["do"]


def test_fahrenheit_converts():
    assert first_stage(temperature_f=425)["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 218.33


def test_over_480f_refused():
    with pytest.raises(CookError, match="480"):
        build(temperature_f=481)
    assert first_stage(temperature_f=480)


def test_249c_accepted():
    assert first_stage(temperature_c=249)["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 249
    with pytest.raises(CookError):
        build(temperature_c=249.5)


def test_sous_vide_over_212f_refused():
    with pytest.raises(CookError, match="212"):
        build(temperature_f=213, sous_vide=True)
    do = first_stage(temperature_f=130, sous_vide=True)["do"]
    assert do["temperatureBulbs"]["mode"] == "wet"
    assert do["steamGenerators"] == {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100}}


def test_wet_bulb_cooks_capped_at_212f():
    with pytest.raises(CookError, match="212"):
        build(temperature_f=213, humidity=40)
    with pytest.raises(CookError, match="100"):
        build(temperature_c=100.5, humidity=40)
    assert first_stage(temperature_f=212, humidity=40)["do"]["temperatureBulbs"]["wet"]["setpoint"] == {"celsius": 100}
    assert first_stage(temperature_f=213, steam_percent=40)  # the dry bulb keeps the oven's cap


def test_wet_bulb_cap_holds_at_update_and_after_a_humidity_change():
    wet = first_stage(temperature_c=60, humidity=40)
    with pytest.raises(CookError, match="212"):
        updated_stage(wet, {"temperature_f": 213})
    was_sous_vide = first_stage(temperature_c=60, sous_vide=True)
    lowered = updated_stage(was_sous_vide, {"humidity": 50})
    with pytest.raises(CookError, match="212"):
        updated_stage(lowered, {"temperature_f": 213})
    with pytest.raises(CookError, match="212"):
        updated_stage(was_sous_vide, {"humidity": 50, "temperature_f": 213})
    assert updated_stage(lowered, {"temperature_f": 212})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["temperature_c", "temperature_f"])
def test_temperature_must_be_a_real_number(field, value):
    with pytest.raises(CookError, match="real number"):
        build(**{field: value})
    with pytest.raises(CookError, match="real number"):
        updated_stage(first_stage(temperature_c=60), {field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["probe_c", "probe_f"])
def test_probe_must_be_a_real_number(field, value):
    with pytest.raises(CookError, match="real number"):
        build(temperature_c=60, **{field: value})


@pytest.mark.parametrize("fan", [0, 0.5])
def test_fan_off_refused_during_a_cook(fan):
    with pytest.raises(CookError, match="only proven"):
        build(temperature_c=60, fan=fan)
    with pytest.raises(CookError, match="only proven"):
        updated_stage(first_stage(temperature_c=60), {"fan": fan})
    assert first_stage(temperature_c=60, fan=1)["do"]["fan"] == {"speed": 1}


def test_timer_and_probe_refused(probe_proven):
    with pytest.raises(CookError):
        build(temperature_c=60, minutes=5, probe_c=50)


def test_both_units_refused(probe_proven):
    with pytest.raises(CookError):
        build(temperature_c=60, temperature_f=140)
    with pytest.raises(CookError):
        build(probe_c=50, probe_f=122, temperature_c=60)


def test_no_temperature_refused():
    with pytest.raises(CookError):
        build(minutes=5)


def test_limit_default_180():
    assert limit_minutes(CookRequest(temperature_c=60)) == 180
    assert limit_minutes(CookRequest(temperature_c=60, max_minutes=45)) == 45


def test_limit_timer_plus_margin():
    from custom_components.jelly_anova.cook import TIMER_MARGIN_MINUTES
    assert TIMER_MARGIN_MINUTES == 10
    assert limit_minutes(CookRequest(temperature_c=60, minutes=30)) == 40


def test_limit_adds_preheat_allowance_when_timer_waits():
    from custom_components.jelly_anova.cook import PREHEAT_ALLOWANCE_MINUTES
    req = CookRequest(temperature_c=60, minutes=30, timer_mode="When Preheated")
    assert limit_minutes(req) == 30 + 10 + PREHEAT_ALLOWANCE_MINUTES


def test_timer_immediately_has_no_entry():
    assert "entry" not in first_stage(temperature_c=60, minutes=5)["do"]["timer"]


def test_timer_when_preheated():
    timer = first_stage(temperature_c=60, minutes=5, timer_mode="When Preheated")["do"]["timer"]
    assert timer["entry"] == {"conditions": {"or": {"nodes.temperatureBulbs.dry.current.celsius": {">=": 60}}}}


def test_wet_preheat_refused_until_proven():
    from custom_components.jelly_anova import cook
    assert cook.WET_PREHEAT_PROVEN is False
    with pytest.raises(CookError, match="not tested"):
        build(temperature_c=60, minutes=5, timer_mode="When Preheated", humidity=50)
    with pytest.raises(CookError, match="not tested"):
        build(temperature_f=130, minutes=5, timer_mode="When Preheated", sous_vide=True)
    assert first_stage(temperature_c=60, minutes=5, humidity=50, timer_mode="Manually")


def test_timer_when_preheated_uses_wet_bulb_for_humidity(monkeypatch):
    monkeypatch.setattr("custom_components.jelly_anova.cook.WET_PREHEAT_PROVEN", True)
    timer = first_stage(temperature_c=60, minutes=5, timer_mode="When Preheated", humidity=50)["do"]["timer"]
    assert timer["entry"] == {"conditions": {"or": {"nodes.temperatureBulbs.wet.current.celsius": {">=": 60}}}}


def test_timer_manually():
    timer = first_stage(temperature_c=60, minutes=5, timer_mode="Manually")["do"]["timer"]
    assert timer["entry"] == {"conditions": {"or": {"userAction": {"=": True}}}}


@pytest.mark.parametrize("mode", ["When Food Detected", "When Food Removed", "Whenever"])
def test_unproven_timer_modes_refused(mode):
    with pytest.raises(CookError):
        build(temperature_c=60, minutes=5, timer_mode=mode)


def test_timer_mode_without_minutes_refused():
    with pytest.raises(CookError):
        build(temperature_c=60, timer_mode="Manually")


def test_steam_and_humidity():
    do = first_stage(temperature_c=60, steam_percent=30)["do"]
    assert do["steamGenerators"] == {"mode": "steam-percentage", "steamPercentage": {"setpoint": 30}}
    assert do["temperatureBulbs"]["mode"] == "dry"
    do = first_stage(temperature_c=60, humidity=60)["do"]
    assert do["steamGenerators"] == {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 60}}
    assert do["temperatureBulbs"] == {"mode": "wet", "wet": {"setpoint": {"celsius": 60}}}
    with pytest.raises(CookError):
        build(temperature_c=60, humidity=60, steam_percent=30)
    with pytest.raises(CookError):
        build(temperature_c=60, steam_percent=101)


def test_elements_fan_vent_rack():
    stage = build(temperature_c=60, elements=frozenset({"top", "bottom"}), fan=50, vent="open-mid", rack=2)[0]
    s = stage["stages"][0]
    assert s["do"]["heatingElements"] == {"top": {"on": True}, "bottom": {"on": True}, "rear": {"on": False}}
    assert s["do"]["fan"] == {"speed": 50}
    assert s["do"]["exhaustVent"] == {"state": "open-mid"}
    assert s["rackPosition"] == 2 and stage["rackPosition"] == 2


def test_all_elements_off_refused():
    with pytest.raises(CookError, match="element"):
        build(temperature_c=60, elements=frozenset())


def test_bad_values_refused():
    for kw in ({"vent": "open"}, {"fan": 101}, {"fan": -1}, {"elements": frozenset({"side"})}, {"minutes": 0}):
        with pytest.raises(CookError):
            build(temperature_c=60, **kw)


def test_updated_stage_keeps_unknown_fields():
    stage = first_stage(temperature_c=60, minutes=5)
    stage["do"]["mystery"] = {"x": 1}
    stage["extra"] = "kept"
    before = copy.deepcopy(stage)
    new = updated_stage(stage, {"vent": "open-max", "fan": 50, "steam_percent": 20, "timer_seconds": 240,
                                "elements": {"top"}})
    assert stage == before  # input untouched
    assert new["do"]["mystery"] == {"x": 1} and new["extra"] == "kept"
    assert new["do"]["exhaustVent"]["state"] == "open-max"
    assert new["do"]["fan"]["speed"] == 50
    assert new["do"]["steamGenerators"]["steamPercentage"]["setpoint"] == 20
    assert new["do"]["timer"]["initial"] == 240
    assert new["do"]["heatingElements"] == {"top": {"on": True}, "bottom": {"on": False}, "rear": {"on": False}}


def test_updated_stage_start_timer_removes_entry():
    stage = first_stage(temperature_c=60, minutes=5, timer_mode="Manually")
    assert "entry" in stage["do"]["timer"]
    new = updated_stage(stage, {"start_timer": True})
    assert "entry" not in new["do"]["timer"] and new["do"]["timer"]["initial"] == 300


def test_updated_stage_humidity_needs_wet_bulb():
    dry = first_stage(temperature_c=60)
    with pytest.raises(CookError):
        updated_stage(dry, {"humidity": 50})
    wet = first_stage(temperature_c=60, humidity=40)
    new = updated_stage(wet, {"humidity": 70})
    assert new["do"]["steamGenerators"] == {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 70}}


def test_updated_stage_refuses_bad_values():
    stage = first_stage(temperature_c=60)
    for ch in ({"vent": "open"}, {"fan": 101}, {"elements": set()}, {"timer_seconds": 0}, {"bogus": 1}):
        with pytest.raises(CookError):
            updated_stage(stage, ch)


def test_updated_stage_fahrenheit():
    stage = first_stage(temperature_c=60, minutes=5, timer_mode="When Preheated")
    entry = copy.deepcopy(stage["do"]["timer"]["entry"])
    stage["entry"] = {"keep": "me"}
    new = updated_stage(stage, {"temperature_f": 425})
    assert new["do"]["temperatureBulbs"]["dry"]["setpoint"] == {"celsius": 218.33}
    assert new["entry"] == {"keep": "me"} and new["do"]["timer"]["entry"] == entry
    assert updated_stage(stage, {"temperature_c": 100})["do"]["temperatureBulbs"]["dry"]["setpoint"] == {"celsius": 100}
    with pytest.raises(CookError):
        updated_stage(stage, {"temperature_c": 100, "temperature_f": 212})


def test_updated_stage_over_480f_refused():
    stage = first_stage(temperature_c=60)
    with pytest.raises(CookError, match="480"):
        updated_stage(stage, {"temperature_f": 481})
    with pytest.raises(CookError):
        updated_stage(stage, {"temperature_c": 249.5})
    assert updated_stage(stage, {"temperature_c": 249})
    sv = first_stage(temperature_f=130, sous_vide=True)
    with pytest.raises(CookError, match="212"):
        updated_stage(sv, {"temperature_f": 213})


def test_updated_stage_wet_bulb():
    stage = first_stage(temperature_c=60, humidity=40)
    new = updated_stage(stage, {"temperature_f": 150})
    bulbs = new["do"]["temperatureBulbs"]
    assert bulbs["mode"] == "wet" and bulbs["wet"]["setpoint"] == {"celsius": 65.56}
    assert "dry" not in bulbs


def test_updated_stage_timer_seconds_needs_a_timer():
    with pytest.raises(CookError, match="timer"):
        updated_stage(first_stage(temperature_c=60), {"timer_seconds": 60})


def test_updated_stage_steam_percent_refused_on_wet_bulb():
    with pytest.raises(CookError, match="humidity"):
        updated_stage(first_stage(temperature_c=60, humidity=40), {"steam_percent": 20})


@pytest.mark.parametrize("minutes", [1.5, 0.5, True, "5", float("nan"), float("inf")])
def test_minutes_must_be_whole(minutes):
    with pytest.raises(CookError):
        build(temperature_c=60, minutes=minutes)


def test_whole_float_minutes_accepted():
    assert first_stage(temperature_c=60, minutes=5.0)["do"]["timer"]["initial"] == 300


def test_probe_temperature_range_checked(probe_proven):
    for kw in ({"probe_c": 250}, {"probe_f": 481}, {"probe_c": 0}, {"probe_c": -5}):
        with pytest.raises(CookError):
            build(temperature_c=60, **kw)
    assert first_stage(temperature_c=60, probe_c=249)


@pytest.mark.parametrize("seconds", [0.5, 0, 1.5, True, "9", float("nan"), float("inf")])
def test_updated_stage_timer_seconds_must_be_whole(seconds):
    with pytest.raises(CookError):
        updated_stage(first_stage(temperature_c=60, minutes=5), {"timer_seconds": seconds})


def test_updated_stage_whole_float_seconds_accepted():
    new = updated_stage(first_stage(temperature_c=60, minutes=5), {"timer_seconds": 90.0})
    assert new["do"]["timer"]["initial"] == 90
