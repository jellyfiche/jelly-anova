"""Turn an Anova oven report into a plain, typed snapshot.

Pure Python: no Home Assistant imports. Every lookup tolerates a missing
node and gives None, so a partial or changed report never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .const import END_STAGE_TITLE

# The dry bulb reads 0 for a moment around starts and stops. A 0 straight
# after a real reading is held back this long; after that it is accepted.
GLITCH_HOLD = timedelta(seconds=5)
# A previous reading below this can really fall to 0, so it is never held.
GLITCH_MIN_PREVIOUS_C = 5.0

_ELEMENTS = ("top", "bottom", "rear")
_STEAM = ("boiler", "evaporator")


@dataclass(frozen=True)
class CookInfo:
    """The cook section of a report."""

    cook_id: str | None
    origin: str | None
    stage_index: int | None
    stage_count: int
    stage_mode: str | None
    started: datetime | None
    stages: list[dict]
    active_stage: dict
    timer_mode: str | None
    timer_initial: int | None
    sous_vide: bool
    rack: int | None
    in_end_stage: bool
    timer_started: datetime | None = None
    # The timer node is idle and carries `initial`: set, waiting to be started. An ended timer's
    # node is just {mode: idle}, while the stage still holds do.timer.initial.
    timer_waiting: bool = False


@dataclass(frozen=True)
class OvenState:
    """One parsed report. Fields are None when the report lacks them."""

    updated: datetime
    mode: str | None = None
    dry_c: float | None = None
    wet_c: float | None = None
    dry_top_c: float | None = None
    dry_bottom_c: float | None = None
    target_c: float | None = None
    probe_c: float | None = None
    target_probe_c: float | None = None
    probe_connected: bool | None = None
    door_closed: bool | None = None
    vent_closed: bool | None = None
    water_low: bool | None = None
    water_empty: bool | None = None
    water_removed: bool | None = None
    waste_full: bool | None = None
    waste_removed: bool | None = None
    lamp_on: bool | None = None
    oven_empty: bool | None = None
    descale_needed: bool | None = None
    any_failed: bool | None = None
    watts: dict[str, int] = field(default_factory=dict)
    usage_hours: dict[str, float] = field(default_factory=dict)
    steam_mode: str | None = None
    humidity: float | None = None
    target_humidity: float | None = None
    fan_speed: str | None = None
    boiler_c: float | None = None
    evaporator_c: float | None = None
    firmware: str | None = None
    firmware_updated: datetime | None = None
    online: bool | None = None
    vent_state: str | None = None
    steam_percent: float | None = None
    target_steam_percent: float | None = None
    door_lamp_preference: str | None = None
    cavity_overheated: bool | None = None
    triacs_failed: bool | None = None
    fan_speeds: dict[str, str] = field(default_factory=dict)
    display_board_c: float | None = None
    dc12v_faults: int | None = None
    hardware: str | None = None
    mains_volts: float | None = None
    mains_hertz: float | None = None
    last_disconnected: datetime | None = None
    food: list[dict] = field(default_factory=list)
    failed: dict[str, bool] = field(default_factory=dict)
    lamp_failed: bool | None = None
    boiler_pump_faults: int | None = None
    boiler_valve_faults: int | None = None
    cook: CookInfo | None = None
    # When dry_c was last a real reading (not a held-back zero).
    dry_good_at: datetime | None = None


def _get(obj: Any, *path: str) -> Any:
    """Walk nested dicts; None as soon as anything is missing."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _celsius(node: Any) -> float | None:
    return _num(_get(node, "current", "celsius"))


def _any_failed(nodes: dict) -> bool | None:
    seen = []
    for name in _ELEMENTS:
        seen.append(_flag(_get(nodes, "heatingElements", name, "failed")))
    for name in _STEAM:
        seen.append(_flag(_get(nodes, "steamGenerators", name, "failed")))
    seen.append(_flag(_get(nodes, "fan", "failed")))
    known = [v for v in seen if v is not None]
    if not known:
        return None
    return any(known)


def _failed(nodes: dict) -> dict[str, bool]:
    found: dict[str, bool] = {}
    for name in _ELEMENTS:
        found[name] = _flag(_get(nodes, "heatingElements", name, "failed"))
    for name in _STEAM:
        found[name] = _flag(_get(nodes, "steamGenerators", name, "failed"))
    found["fan"] = _flag(_get(nodes, "fan", "failed"))
    return {k: v for k, v in found.items() if v is not None}


def _count(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _fan_speeds(nodes: dict) -> dict[str, str]:
    speeds: dict[str, str] = {}
    for key, node in (
        ("exhaust", "exhaustFan"),
        ("led", "ledFan"),
        ("display", "displayFan"),
    ):
        speed = _text(_get(nodes, node, "speed"))
        if speed is not None:
            speeds[key] = speed
    power = _flag(_get(nodes, "powerBoardFan", "on"))
    if power is not None:
        speeds["power_board"] = "on" if power else "off"
    # Keep a stable order.
    order = ("exhaust", "led", "power_board", "display")
    return {k: speeds[k] for k in order if k in speeds}


def _cook(raw: Any, nodes: dict) -> CookInfo | None:
    if not isinstance(raw, dict):
        return None
    all_stages = [s for s in (raw.get("stages") or []) if isinstance(s, dict)]
    stages = [s for s in all_stages if s.get("title") != END_STAGE_TITLE]
    index = raw.get("activeStageIndex")
    index = index if isinstance(index, int) and not isinstance(index, bool) else None
    active: dict = {}
    if index is not None and 0 <= index < len(all_stages):
        active = all_stages[index]
    else:
        wanted = raw.get("activeStageId")
        for stage in all_stages:
            if wanted is not None and stage.get("id") == wanted:
                active = stage
                break
    stage_timer = _get(active, "do", "timer")
    has_timer = isinstance(stage_timer, dict)
    timer_initial = _num(stage_timer.get("initial")) if has_timer else None
    # Wet-bulb mode at any humidity, as andr83's sensor meant it.
    sous_vide = _get(active, "do", "temperatureBulbs", "mode") == "wet"
    rack = raw.get("rackPosition")
    return CookInfo(
        cook_id=_text(raw.get("cookId")),
        origin=_text(raw.get("originSource")),
        stage_index=index,
        stage_count=len(stages),
        stage_mode=_text(raw.get("activeStageMode")),
        started=_time(raw.get("startedTimestamp")),
        stages=stages,
        active_stage=active,
        timer_mode=_text(_get(nodes, "timer", "mode")) if has_timer else None,
        timer_initial=int(timer_initial) if timer_initial is not None else None,
        sous_vide=sous_vide,
        rack=rack if isinstance(rack, int) and not isinstance(rack, bool) else None,
        in_end_stage=active.get("title") == END_STAGE_TITLE if active else False,
        timer_started=_time(_get(nodes, "timer", "startedAtTimestamp")) if has_timer else None,
        timer_waiting=has_timer
        and _get(nodes, "timer", "mode") == "idle"
        and _num(_get(nodes, "timer", "initial")) is not None,
    )


def parse_state(payload: dict, previous: OvenState | None) -> OvenState:
    """Parse the payload of an EVENT_APO_STATE message.

    `payload` is the message's payload (the report is at payload["state"]);
    the report itself is accepted too. `previous` lets the dry-bulb zero
    glitch be filtered; pass None for the first report.
    """
    payload = payload if isinstance(payload, dict) else {}
    report = payload if "nodes" in payload else payload.get("state")
    report = report if isinstance(report, dict) else {}
    nodes = report.get("nodes")
    nodes = nodes if isinstance(nodes, dict) else {}
    info = report.get("systemInfo")
    info = info if isinstance(info, dict) else {}
    top = report.get("state")
    top = top if isinstance(top, dict) else {}

    updated = _time(report.get("updatedTimestamp")) or datetime.now(UTC)

    bulbs_mode = _text(_get(nodes, "temperatureBulbs", "mode"))
    target_c = None
    if bulbs_mode in ("dry", "wet"):
        target_c = _num(_get(nodes, "temperatureBulbs", bulbs_mode, "setpoint", "celsius"))

    watts: dict[str, int] = {}
    hours: dict[str, float] = {}
    for key in _ELEMENTS:
        w = _num(_get(nodes, "heatingElements", key, "watts"))
        if w is not None:
            watts[key] = int(w)
        h = _num(_get(nodes, "heatingElements", key, "usageHours"))
        if h is not None:
            hours[key] = float(h)
    for key in _STEAM:
        w = _num(_get(nodes, "steamGenerators", key, "watts"))
        if w is not None:
            watts[key] = int(w)
        h = _num(_get(nodes, "steamGenerators", key, "usageHours"))
        if h is not None:
            hours[key] = float(h)

    detection = _get(nodes, "cavityCamera", "detection")
    food = [d for d in detection if isinstance(d, dict)] if isinstance(detection, list) else []

    probe_connected = _flag(_get(nodes, "temperatureProbe", "connected"))
    probe_c = _celsius(_get(nodes, "temperatureProbe")) if probe_connected else None

    vent_state = _text(_get(nodes, "exhaustVent", "state"))

    dry_c = _celsius(_get(nodes, "temperatureBulbs", "dry"))
    dry_good_at = updated if dry_c is not None else None
    if (
        dry_c == 0
        and previous is not None
        and previous.dry_c is not None
        and previous.dry_c >= GLITCH_MIN_PREVIOUS_C
        and previous.dry_good_at is not None
        and updated - previous.dry_good_at <= GLITCH_HOLD
    ):
        dry_c = previous.dry_c
        dry_good_at = previous.dry_good_at

    return OvenState(
        updated=updated,
        mode=_text(top.get("mode")),
        dry_c=dry_c,
        wet_c=_celsius(_get(nodes, "temperatureBulbs", "wet")),
        dry_top_c=_celsius(_get(nodes, "temperatureBulbs", "dryTop")),
        dry_bottom_c=_celsius(_get(nodes, "temperatureBulbs", "dryBottom")),
        target_c=target_c,
        probe_c=probe_c,
        target_probe_c=_num(_get(nodes, "temperatureProbe", "setpoint", "celsius")),
        probe_connected=probe_connected,
        door_closed=_flag(_get(nodes, "door", "closed")),
        vent_closed=(vent_state == "closed") if vent_state is not None else None,
        water_low=_flag(_get(nodes, "waterTank", "low")),
        water_empty=_flag(_get(nodes, "waterTank", "empty")),
        water_removed=_flag(_get(nodes, "waterTank", "removed")),
        waste_full=_flag(_get(nodes, "wasteWaterTank", "full")),
        waste_removed=_flag(_get(nodes, "wasteWaterTank", "removed")),
        lamp_on=_flag(_get(nodes, "cavityLamp", "on")),
        oven_empty=_flag(_get(nodes, "cavityCamera", "isEmpty")),
        descale_needed=_flag(_get(nodes, "steamGenerators", "boiler", "descaleRequired")),
        any_failed=_any_failed(nodes),
        watts=watts,
        usage_hours=hours,
        steam_mode=_text(_get(nodes, "steamGenerators", "mode")),
        humidity=_num(_get(nodes, "steamGenerators", "relativeHumidity", "current")),
        target_humidity=_num(_get(nodes, "steamGenerators", "relativeHumidity", "setpoint")),
        fan_speed=_text(_get(nodes, "fan", "speed")),
        boiler_c=_num(_get(nodes, "steamGenerators", "boiler", "celsius")),
        evaporator_c=_num(_get(nodes, "steamGenerators", "evaporator", "celsius")),
        firmware=_text(info.get("firmwareVersion")),
        firmware_updated=_time(info.get("firmwareUpdatedTimestamp")),
        online=_flag(info.get("online")),
        vent_state=vent_state,
        steam_percent=_num(_get(nodes, "steamGenerators", "steamPercentage", "current")),
        target_steam_percent=_num(_get(nodes, "steamGenerators", "steamPercentage", "setpoint")),
        door_lamp_preference=_text(_get(nodes, "doorLamp", "preferences")),
        cavity_overheated=_flag(top.get("cavityOverheated")),
        triacs_failed=_flag(info.get("triacsFailed")),
        fan_speeds=_fan_speeds(nodes),
        display_board_c=_num(_get(nodes, "displayBoard", "celsius")),
        dc12v_faults=_num(_get(nodes, "dc12VLine", "numberOfFaults")),
        hardware=_text(info.get("hardwareVersion")),
        mains_volts=_num(info.get("powerMains")),
        mains_hertz=_num(info.get("powerHertz")),
        last_disconnected=_time(info.get("lastDisconnectedTimestamp")),
        food=food,
        failed=_failed(nodes),
        lamp_failed=_flag(_get(nodes, "cavityLamp", "failed")),
        boiler_pump_faults=_count(_get(nodes, "steamGenerators", "boiler", "numberOfOverCurrentInletPump")),
        boiler_valve_faults=_count(_get(nodes, "steamGenerators", "boiler", "numberOfOverCurrentOutletValveDescale")),
        cook=_cook(report.get("cook"), nodes),
        dry_good_at=dry_good_at,
    )


def is_newer(state: OvenState, than: datetime) -> bool:
    """True when the report is strictly newer than `than`."""
    return state.updated > than
