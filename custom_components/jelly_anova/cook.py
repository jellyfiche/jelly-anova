"""Build cook commands for the Anova Precision Oven 2.0. Pure Python, no Home Assistant imports.

Every field shape here was accepted by the real oven (spike/battery.py, protocol-notes.md).
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from .const import DEFAULT_MAX_MINUTES, END_STAGE_TITLE, MAX_C, MAX_F

TIMER_MARGIN_MINUTES = 10  # saved stop time = timer + this, so the oven's own end comes first
PREHEAT_ALLOWANCE_MINUTES = 60  # extra wait allowed before a timer that starts on preheat or by hand
END_STAGE_SECONDS = 12 * 3600  # the hidden last stage's timer
END_STAGE_CELSIUS = 25
SOUS_VIDE_MAX_F = 212
SOUS_VIDE_MAX_C = 100.0
ELEMENTS = ("top", "bottom", "rear")
VENTS = ("closed", "open-mid", "open-max")
PROVEN_TIMER_MODES = ("Immediately", "When Preheated", "Manually")
FOOD_TIMER_MODES = ("When Food Detected", "When Food Removed")
RACK_RANGE = range(1, 6)
UPDATE_KEYS = {"start_timer", "vent", "steam_percent", "humidity", "fan", "timer_seconds", "elements"}


class CookError(ValueError):
    """A cook request the oven would refuse or that is not safe to send."""


@dataclass
class CookRequest:
    temperature_f: float | None = None
    temperature_c: float | None = None
    elements: frozenset[str] = frozenset({"rear"})
    steam_percent: int | None = None
    humidity: int | None = None
    sous_vide: bool = False
    minutes: int | None = None
    timer_mode: str = "Immediately"
    probe_f: float | None = None
    probe_c: float | None = None
    fan: int = 100
    vent: str = "closed"
    rack: int = 3
    max_minutes: int | None = None


def _f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 2)


def _celsius(c: float | None, f: float | None, what: str) -> float | None:
    if c is not None and f is not None:
        raise CookError(f"Give the {what} in Celsius or Fahrenheit, not both.")
    if f is not None:
        if what == "oven temperature" and f > MAX_F:
            raise CookError(f"The oven cannot go above {MAX_F} F ({MAX_C:g} C).")
        return _f_to_c(f)
    return c


def _check_elements(elements) -> dict:
    names = set(elements)
    if not names:
        raise CookError("At least one heating element must be on: the oven refuses a stage with all three off.")
    if not names <= set(ELEMENTS):
        raise CookError("Heating elements can only be top, bottom and rear.")
    return {name: {"on": name in names} for name in ELEMENTS}


def _check_vent(vent: str) -> str:
    if vent not in VENTS:
        raise CookError("The vent can only be closed, open-mid or open-max.")
    return vent


def _check_fan(fan) -> int:
    if isinstance(fan, bool) or not isinstance(fan, (int, float)) or not 0 <= fan <= 100:
        raise CookError("Fan speed must be 0 to 100.")
    return int(fan)


def _check_percent(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise CookError(f"{what} must be 0 to 100.")
    return int(value)


def _timer_entry(mode: str, bulb: str, setpoint_c: float) -> dict | None:
    if mode == "Immediately":
        return None
    if mode == "When Preheated":
        return {"conditions": {"or": {f"nodes.temperatureBulbs.{bulb}.current.celsius": {">=": setpoint_c}}}}
    if mode == "Manually":
        return {"conditions": {"or": {"userAction": {"=": True}}}}
    if mode in FOOD_TIMER_MODES:
        raise CookError(f'"{mode}" has not been tested on the oven yet, so it is not offered.')
    raise CookError(f'Unknown timer mode "{mode}". Use Immediately, When Preheated or Manually.')


def _stage_id() -> str:
    return f"ios-{uuid.uuid4()}"


def _stage(celsius, seconds, bulb, steam, fan, vent, rack, elements, entry=None, probe_c=None) -> dict:
    do: dict[str, Any] = {
        "type": "cook",
        "fan": {"speed": fan},
        "heatingElements": elements,
        "exhaustVent": {"state": vent},
        "steamGenerators": steam,
        "temperatureBulbs": {"mode": bulb, bulb: {"setpoint": {"celsius": celsius}}},
    }
    if seconds is not None:
        timer: dict[str, Any] = {"initial": seconds}
        if entry:
            timer["entry"] = entry
        do["timer"] = timer
    if probe_c is not None:
        # Untested on the 2.0: the probe target is built as the published schemas describe it.
        do["temperatureProbe"] = {"setpoint": {"celsius": probe_c}}
    stage: dict[str, Any] = {"id": _stage_id(), "title": "", "description": "", "rackPosition": rack, "do": do}
    if seconds is not None:
        stage["exit"] = {"conditions": {"and": {"nodes.timer.mode": {"=": "completed"}}}}
    return stage


def _validate(req: CookRequest):
    celsius = _celsius(req.temperature_c, req.temperature_f, "oven temperature")
    if celsius is None:
        raise CookError("Give an oven temperature.")
    if celsius > MAX_C:
        raise CookError(f"The oven cannot go above {MAX_F} F ({MAX_C:g} C).")
    if celsius <= 0:
        raise CookError("The oven temperature must be above zero.")
    probe = _celsius(req.probe_c, req.probe_f, "probe temperature")
    if req.minutes is not None and probe is not None:
        raise CookError("A cook can end on a timer or on a probe temperature, not both.")
    if req.minutes is not None and (isinstance(req.minutes, bool) or req.minutes <= 0):
        raise CookError("The timer must be at least one minute.")
    if req.minutes is None and req.timer_mode != "Immediately":
        _timer_entry(req.timer_mode, "dry", celsius)  # raises for unknown or food modes
        raise CookError("A timer mode needs a timer: give the minutes.")
    if req.steam_percent is not None and req.humidity is not None:
        raise CookError("Give steam or humidity, not both.")
    if req.sous_vide:
        if celsius > SOUS_VIDE_MAX_C:
            raise CookError(f"Sous vide cannot go above {SOUS_VIDE_MAX_F} F (100 C).")
        if req.steam_percent is not None or req.humidity not in (None, 100):
            raise CookError("Sous vide runs at 100% humidity; steam and other humidity values do not apply.")
    if req.rack not in RACK_RANGE:
        raise CookError("The rack position must be 1 to 5.")
    return celsius, probe


def limit_minutes(req: CookRequest) -> int:
    """The longest this cook may run, in minutes. Its own maximum wins; else the timer plus a margin."""
    if req.max_minutes is not None:
        return int(req.max_minutes)
    if req.minutes is None:
        return DEFAULT_MAX_MINUTES
    total = int(req.minutes) + TIMER_MARGIN_MINUTES
    if req.timer_mode != "Immediately":
        total += PREHEAT_ALLOWANCE_MINUTES
    return total


def build_start(req: CookRequest, cooker_id: str) -> tuple[dict, str]:
    """Return the CMD_APO_START payload and its cook id."""
    celsius, probe = _validate(req)
    humid = req.sous_vide or req.humidity is not None
    bulb = "wet" if humid else "dry"
    if humid:
        steam = {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100 if req.sous_vide else _check_percent(req.humidity, "Humidity")}}
    else:
        steam = {"mode": "steam-percentage", "steamPercentage": {"setpoint": _check_percent(req.steam_percent or 0, "Steam")}}
    elements = _check_elements(req.elements)
    fan, vent = _check_fan(req.fan), _check_vent(req.vent)
    seconds = None if req.minutes is None else int(req.minutes) * 60
    entry = _timer_entry(req.timer_mode, bulb, celsius)
    stages = [_stage(celsius, seconds, bulb, steam, fan, vent, req.rack, elements, entry, probe)]
    if seconds is not None:
        # Cooks do not end by themselves (design.md, "Ending cooks safely"): a hidden last stage
        # at 25 C keeps the elements at 0 W if Home Assistant is down when the timer ends.
        end = _stage(
            END_STAGE_CELSIUS, END_STAGE_SECONDS, "dry",
            {"mode": "steam-percentage", "steamPercentage": {"setpoint": 0}},
            100, "closed", req.rack, {"top": {"on": False}, "bottom": {"on": False}, "rear": {"on": True}},
        )
        end["title"] = END_STAGE_TITLE
        stages.append(end)
    cook_id = f"ios-{uuid.uuid4()}"
    payload = {
        "stages": stages, "cookId": cook_id, "cookerId": cooker_id, "cookableId": "", "title": "",
        "type": "oven_v2", "originSource": "ios", "cookableType": "manual", "rackPosition": req.rack,
    }
    return payload, cook_id


def updated_stage(active_stage: dict, changes: dict) -> dict:
    """Copy the active stage with the changes applied; every other field is kept as the oven sent it."""
    unknown = set(changes) - UPDATE_KEYS
    if unknown:
        raise CookError(f"Cannot change {', '.join(sorted(unknown))} during a cook.")
    stage = copy.deepcopy(active_stage)
    do = stage.setdefault("do", {})
    if "vent" in changes:
        do.setdefault("exhaustVent", {})["state"] = _check_vent(changes["vent"])
    if "fan" in changes:
        do.setdefault("fan", {})["speed"] = _check_fan(changes["fan"])
    if "elements" in changes:
        wanted = _check_elements(changes["elements"])
        current = do.setdefault("heatingElements", {})
        for name, value in wanted.items():
            current.setdefault(name, {}).update(value)
    if "steam_percent" in changes:
        pct = _check_percent(changes["steam_percent"], "Steam")
        do["steamGenerators"] = {"mode": "steam-percentage", "steamPercentage": {"setpoint": pct}}
    if "humidity" in changes:
        if do.get("temperatureBulbs", {}).get("mode") != "wet":
            raise CookError("Humidity needs a cook started with humidity or sous vide (wet bulb).")
        pct = _check_percent(changes["humidity"], "Humidity")
        do["steamGenerators"] = {"mode": "relative-humidity", "relativeHumidity": {"setpoint": pct}}
    if "timer_seconds" in changes:
        seconds = changes["timer_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise CookError("The timer must be at least one second.")
        do.setdefault("timer", {})["initial"] = int(seconds)
    if changes.get("start_timer"):
        do.get("timer", {}).pop("entry", None)
    return stage
