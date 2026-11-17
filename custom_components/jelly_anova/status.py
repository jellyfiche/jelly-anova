"""Where the running cook is, in words: preheating, waiting for you, cooking, timer finished.

Pure Python: no Home Assistant imports. Read from the active stage the oven reports:

- A stage whose entry is not met yet is "entering": preheating, or waiting for the probe or for
  you to start it.
- A timer waiting to start is an idle timer node that still carries `initial`; its entry says on
  what (you, food going in, or the oven reaching a temperature).
- A timer that ran out is an idle node without `initial`, or `completed`. The stage's exit then
  says whether the oven waits for you or for the food to come out.

The hidden last stage a Home Assistant cook ends with reads "finished".
"""

from __future__ import annotations

from typing import Any

from .state import CookInfo, OvenState

IDLE = "idle"
DESCALING = "descaling"
PREHEATING = "preheating"
PLUG_IN_PROBE = "plug_in_probe"
START_TIMER = "start_timer"
ADD_FOOD = "add_food"
COOKING = "cooking"
START_NEXT_STAGE = "start_next_stage"
REMOVE_FOOD = "remove_food"
TIMER_DONE = "timer_done"
FINISHED = "finished"

OPTIONS = [
    IDLE,
    DESCALING,
    PREHEATING,
    PLUG_IN_PROBE,
    START_TIMER,
    ADD_FOOD,
    COOKING,
    START_NEXT_STAGE,
    REMOVE_FOOD,
    TIMER_DONE,
    FINISHED,
]

# The states the Continue button moves on from
WAITING_FOR_YOU = (START_TIMER, ADD_FOOD, START_NEXT_STAGE)

USER_ACTION = "userAction"
CAVITY_EMPTY = "nodes.cavityCamera.isEmpty"
PROBE_CONNECTED = "nodes.temperatureProbe.connected"
PROBE_NODE = "nodes.temperatureProbe."
_LOGIC = ("and", "or", "not")


def condition_paths(conditions: Any) -> set[str]:
    """Every node path a condition block tests, however its and/or groups are nested."""
    found: set[str] = set()
    if not isinstance(conditions, dict):
        return found
    for key, value in conditions.items():
        if key in _LOGIC:
            found |= condition_paths(value)
        elif isinstance(key, str):
            found.add(key)
    return found


def _paths(stage: dict, *where: str) -> set[str]:
    node: Any = stage
    for key in where:
        node = node.get(key) if isinstance(node, dict) else None
    return condition_paths(node.get("conditions") if isinstance(node, dict) else None)


def _timer_ended(cook: CookInfo) -> bool:
    if cook.timer_mode == "completed":
        return True
    return cook.timer_mode == "idle" and not cook.timer_waiting


def cook_status(state: OvenState | None) -> str | None:
    """The cook's status, one of OPTIONS, or None when the report does not say."""
    if state is None or state.mode is None:
        return None
    if state.mode == "idle":
        return IDLE
    if state.mode == "descale":
        return DESCALING
    if state.mode != "cook":
        return None
    cook = state.cook
    if cook is None or not cook.active_stage:
        return None
    if cook.in_end_stage:
        return FINISHED
    stage = cook.active_stage

    if cook.stage_mode == "entering":
        entry = _paths(stage, "entry")
        if PROBE_CONNECTED in entry and not state.probe_connected:
            return PLUG_IN_PROBE
        if entry == {USER_ACTION}:
            return START_NEXT_STAGE
        return PREHEATING

    if cook.timer_waiting:
        entry = _paths(stage, "do", "timer", "entry")
        if CAVITY_EMPTY in entry:
            return ADD_FOOD
        if entry == {USER_ACTION}:
            return START_TIMER
        if any(path.startswith("nodes.temperatureBulbs.") for path in entry):
            return PREHEATING
        return COOKING

    exit_paths = _paths(stage, "exit")
    if cook.timer_mode is not None and _timer_ended(cook):
        if CAVITY_EMPTY in exit_paths:
            return REMOVE_FOOD
        if USER_ACTION in exit_paths and next_stage_id(cook) is not None:
            return START_NEXT_STAGE
        return TIMER_DONE

    # Only a stage that ends on the probe waits for it; a probe target beside a timer does not.
    if state.probe_connected is False and any(path.startswith(PROBE_NODE) for path in exit_paths):
        return PLUG_IN_PROBE
    return COOKING


def continue_target(state: OvenState | None) -> tuple[str, str | None] | None:
    """What Continue does now: ("timer", None) releases a waiting timer, ("stage", id) starts that
    stage. None when the oven is not waiting on you.

    A stage that waits on you to begin is started by its own id; a finished stage that waits on
    you moves to the next one. A timer whose start you may give (alone, or with preheating or
    food going in) is released.
    """
    now = cook_status(state)
    if now is None or state is None or state.cook is None:
        return None
    cook = state.cook
    stage = cook.active_stage
    if now == START_NEXT_STAGE:
        if cook.stage_mode == "entering":
            own = stage.get("id")
            return ("stage", own) if isinstance(own, str) else None
        following = next_stage_id(cook)
        return ("stage", following) if following is not None else None
    if now in (START_TIMER, ADD_FOOD, PREHEATING) and cook.timer_waiting and cook.stage_mode != "entering":
        if USER_ACTION in _paths(stage, "do", "timer", "entry"):
            return ("timer", None)
    return None


def next_stage_id(cook: CookInfo) -> str | None:
    """The id of the stage after the active one, or None if it is the last (the hidden end stage
    is not a stage of the cook)."""
    ids = [stage.get("id") for stage in cook.stages]
    current = cook.active_stage.get("id") if cook.active_stage else None
    if current not in ids:
        return None
    index = ids.index(current) + 1
    return ids[index] if index < len(ids) else None
