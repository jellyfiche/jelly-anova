"""Runs the oven: starts and stops cooks, and decides when a real oven is stopped.

The rule this file exists for: a cook Home Assistant started must end, even if reports stop, the
login fails or Home Assistant restarts; and a cook started from the app or the oven's panel is
never stopped automatically. Which cook is ours is decided by its saved cook id and nothing else.

Every automatic stop goes one way: move the saved stop time to now, then let _check_stop_due send
it. So it survives a restart and is retried until it is delivered.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connection import OvenConnection, OvenError, OvenRefused, backup_stop
from .const import (
    DEFAULT_MAX_MINUTES,
    DOMAIN,
    EVENT_COOK_FINISHED,
    EVENT_TARGET_REACHED,
    MAX_C,
    MAX_F,
    STALE_SECONDS,
)
from .cook import CookError, CookRequest, build_start, limit_minutes, updated_stage
from .state import OvenState, is_newer

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
CHECK_SECONDS = 5  # how often the saved stop time and report freshness are checked
HOURS_SAVE_SECONDS = 300  # the running total is written this often, not on every report
STOP_RETRY_SECONDS = 60  # an automatic stop not yet shown to have worked goes out again after this
RESTART_WAIT = 30  # after a restart, how long a due stop waits for a first report
REPORT_WAIT = 20  # a start waits this long for a fresh report (idle reports can be 15 s apart)
IDLE_WAIT = 30  # a start during a cook waits this long for the oven to report idle

# Timer modes that mean it ran out. A missing timer is None, and is never one of these.
TIMER_ENDED = ("idle", "completed")

ISSUE_LOGIN = "login_failed"
ISSUE_STOP = "stop_failed"


def _check_max_minutes(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 1:
        raise CookError("The maximum cook time must be at least 1 minute.")


def _custom_payload(stages: list[dict], cook_id: str, cooker_id: str) -> dict:
    """The CMD_APO_START payload for raw stages, which are sent as written."""
    if not stages or not all(isinstance(stage, dict) for stage in stages):
        raise CookError("A custom cook needs at least one stage.")
    try:
        for stage in stages:
            bulbs = stage.get("do", {}).get("temperatureBulbs", {})
            for bulb in ("dry", "wet"):
                celsius = bulbs.get(bulb, {}).get("setpoint", {}).get("celsius")
                if isinstance(celsius, (int, float)) and celsius > MAX_C:
                    raise CookError(f"The oven cannot go above {MAX_F} F ({MAX_C:g} C).")
    except AttributeError:
        raise CookError("A custom cook's stages are not in the oven's format.") from None
    return {
        "stages": stages,
        "cookId": cook_id,
        "cookerId": cooker_id,
        "cookableId": "",
        "title": "",
        "type": "oven_v2",
        "originSource": "ios",
        "cookableType": "manual",
        "rackPosition": stages[0].get("rackPosition", 3),
    }


class OvenRuntime:
    """Everything that happens between the connection and the entities."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, connection: OvenConnection, session: Any
    ) -> None:
        self._hass = hass
        self._entry = entry
        self.connection = connection
        self._session = session
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.run_task: asyncio.Task | None = None  # connection.run(), set by async_setup_entry

        self.state: OvenState | None = None
        self._connected = False
        self._listeners: list[Callable[[], None]] = []
        self._was_available = False
        self._closed = False
        self._cancel_tick: Callable[[], None] | None = None

        # The cook we started (saved).
        self._cook_id: str | None = None
        self._stop_at: datetime | None = None
        self._cooker_id: str | None = None
        self._timer_seen = False  # its timer has been seen running

        # Sending the stop.
        self._stop_task: asyncio.Task | None = None
        self._next_stop_try: datetime | None = None
        self._hold_until: datetime | None = None
        self._stop_presses = 0
        self._start_lock = asyncio.Lock()
        self._waiters: list[tuple[Callable[[OvenState], bool], asyncio.Future]] = []
        # True at first: signing in again reloads this, and the old issue must still go.
        self._login_issue = True

        # The cook the reports show, whoever started it.
        self._active_id: str | None = None
        self._entering_id: str | None = None
        self._target_done_id: str | None = None
        self._after_restart = False

        self._total_hours = 0.0
        self._hours_from: datetime | None = None
        self._hours_save_at: datetime | None = None

    # --- what entities read --------------------------------------------------

    @property
    def available(self) -> bool:
        return self._connected and self._fresh(self.state)

    @property
    def total_cook_hours(self) -> float:
        return self._total_hours

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` on every change. Returns the function that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        self._was_available = self.available
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:  # noqa: BLE001 - an entity must not stop the safety code
                _LOGGER.warning("A listener failed: %s", type(err).__name__)

    # --- saved data ----------------------------------------------------------

    def _data(self) -> dict:
        return {
            "cook_id": self._cook_id,
            "stop_at": self._stop_at.isoformat() if self._stop_at else None,
            "total_cook_hours": self._total_hours,
            # Kept so the backup stop works after a restart with the login gone.
            "cooker_id": self.connection.cooker_id or self._cooker_id,
        }

    def _save(self) -> None:
        self._hass.async_create_task(self._store.async_save(self._data()), "jelly_anova save")

    async def restore(self) -> None:
        """Load what was saved and start checking the stop time."""
        data = await self._store.async_load()
        data = data if isinstance(data, dict) else {}
        hours = data.get("total_cook_hours")
        if isinstance(hours, (int, float)) and not isinstance(hours, bool):
            self._total_hours = float(hours)
        cooker = data.get("cooker_id")
        self._cooker_id = cooker if isinstance(cooker, str) else None
        cook_id = data.get("cook_id")
        if isinstance(cook_id, str) and cook_id:
            now = dt_util.utcnow()
            stop_at = data.get("stop_at")
            # A cook with an unreadable stop time gets the safe reading: it is due.
            parsed = dt_util.parse_datetime(stop_at) if isinstance(stop_at, str) else None
            self._cook_id = cook_id
            self._stop_at = parsed or now
            self._active_id = cook_id
            # Restored before the connection runs. Sent now, a due stop could end a cook that
            # was started since, so it first gets a chance to see one report.
            self._hold_until = now + timedelta(seconds=RESTART_WAIT)
        self._after_restart = True
        self._cancel_tick = async_track_time_interval(
            self._hass,
            self._tick,
            timedelta(seconds=CHECK_SECONDS),
            name="jelly_anova check",
            cancel_on_shutdown=True,
        )

    async def shutdown(self) -> None:
        """Stop the timer and any stop in flight, and save. A saved stop time stays saved."""
        if self._closed:
            return
        self._closed = True
        if self._cancel_tick is not None:
            self._cancel_tick()
        if self._stop_task is not None and not self._stop_task.done():
            self._stop_task.cancel()
        for _, future in self._waiters:
            if not future.done():
                future.set_result(None)
        await self._store.async_save(self._data())

    # --- from the connection -------------------------------------------------

    @callback
    def on_status(self, up: bool) -> None:
        self._connected = up
        if not up:
            self._hours_from = None
        self._notify()

    @callback
    def on_auth_failed(self) -> None:
        """Anova refused the login. Ask to sign in, and say so loudly during a cook."""
        self._entry.async_start_reauth(self._hass)
        if self._cook_id is not None or self._cook_reported(self.state):
            self._login_issue = True
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                ISSUE_LOGIN,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_LOGIN,
            )

    @callback
    def on_state(self, state: OvenState) -> None:
        """Handle a report. The connection only passes on fresh ones."""
        self.state = state
        if self._login_issue:
            self._login_issue = False
            ir.async_delete_issue(self._hass, DOMAIN, ISSUE_LOGIN)
        self._count_hours(state)
        self._track_cook(state)
        self._check_target(state)
        self._check_timer_end(state)
        self._check_stop_due()
        for wanted, future in list(self._waiters):
            if not future.done() and wanted(state):
                future.set_result(state)
        self._notify()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._check_stop_due()
        if self.available != self._was_available:
            self._notify()

    # --- reading reports -----------------------------------------------------

    @staticmethod
    def _fresh(state: OvenState | None) -> bool:
        if state is None:
            return False
        return dt_util.utcnow() - state.updated <= timedelta(seconds=STALE_SECONDS)

    @staticmethod
    def _cook_reported(state: OvenState | None) -> bool:
        return state is not None and state.mode != "idle"

    def _fresh_idle(self) -> bool:
        return self._fresh(self.state) and self.state.mode == "idle"

    def _other_cook_running(self, cook_id: str) -> bool:
        """True only when a fresh report shows a different cook. Not knowing is not True."""
        state = self.state
        if not self._fresh(state) or state.mode == "idle" or state.cook is None:
            return False
        return state.cook.cook_id is not None and state.cook.cook_id != cook_id

    def _count_hours(self, state: OvenState) -> None:
        cooking = state.mode == "cook"
        if cooking and self._hours_from is not None:
            gap = (state.updated - self._hours_from).total_seconds()
            if 0 < gap <= STALE_SECONDS:
                self._total_hours += gap / 3600
                now = dt_util.utcnow()
                if self._hours_save_at is None or now >= self._hours_save_at:
                    self._hours_save_at = now + timedelta(seconds=HOURS_SAVE_SECONDS)
                    self._store.async_delay_save(self._data, HOURS_SAVE_SECONDS)
        self._hours_from = state.updated if cooking else None

    def _track_cook(self, state: OvenState) -> None:
        """Notice a cook ending, whoever started it."""
        if state.mode == "idle":
            current = None
        elif state.cook is not None and state.cook.cook_id is not None:
            current = state.cook.cook_id
        else:
            return  # some cook is on, which one is not known
        ended, self._active_id = self._active_id, current
        if ended is None or ended == current:
            return
        # Only a cook seen running can end. Until ours has shown up an idle report means nothing,
        # because it can lag a start, so the saved stop time stays.
        if ended == self._cook_id:
            self._forget_cook()
        self._hass.bus.async_fire(EVENT_COOK_FINISHED, {"cook_id": ended})

    def _check_target(self, state: OvenState) -> None:
        after_restart, self._after_restart = self._after_restart, False
        cook = state.cook
        if state.mode == "idle" or cook is None or cook.cook_id is None or cook.in_end_stage:
            return
        if cook.cook_id == self._target_done_id:
            return
        if cook.stage_mode == "entering":
            self._entering_id = cook.cook_id
        left_entering = self._entering_id == cook.cook_id and cook.stage_mode != "entering"
        if left_entering or self._at_setpoint(state):
            self._target_done_id = cook.cook_id
            # Already there on the first report after a restart: it fired before the restart.
            if not after_restart:
                self._hass.bus.async_fire(EVENT_TARGET_REACHED, {"cook_id": cook.cook_id})

    @staticmethod
    def _at_setpoint(state: OvenState) -> bool:
        # From the stage, not the report's setpoint: that one stays from the cook before.
        bulbs = state.cook.active_stage.get("do", {}).get("temperatureBulbs", {})
        bulb = bulbs.get("mode")
        target = bulbs.get(bulb, {}).get("setpoint", {}).get("celsius")
        current = {"dry": state.dry_c, "wet": state.wet_c}.get(bulb)
        return target is not None and current is not None and current >= target

    def _check_timer_end(self, state: OvenState) -> None:
        """For our cook only: has it reached its end?"""
        cook = state.cook
        if self._cook_id is None or state.mode == "idle" or cook is None:
            return
        if cook.cook_id != self._cook_id:
            return
        if cook.stage_count == 1 and cook.timer_mode == "running":
            self._timer_seen = True
        timer_ended = cook.stage_count == 1 and self._timer_seen and cook.timer_mode in TIMER_ENDED
        if cook.in_end_stage or timer_ended:
            now = dt_util.utcnow()
            if self._stop_at is None or self._stop_at > now:
                self._stop_at = now
                self._save()

    # --- the automatic stop --------------------------------------------------

    def _forget_cook(self) -> None:
        self._cook_id = None
        self._stop_at = None
        self._timer_seen = False
        self._next_stop_try = None
        self._hold_until = None
        ir.async_delete_issue(self._hass, DOMAIN, ISSUE_STOP)
        self._save()

    def _check_stop_due(self) -> None:
        if self._closed or self._stop_at is None:
            return
        if self._stop_task is not None and not self._stop_task.done():
            return
        now = dt_util.utcnow()
        if now < self._stop_at:
            return
        if self._next_stop_try is not None and now < self._next_stop_try:
            return
        if self._hold_until is not None and now < self._hold_until and not self._fresh(self.state):
            return
        self._next_stop_try = now + timedelta(seconds=STOP_RETRY_SECONDS)
        self._stop_task = self._entry.async_create_background_task(
            self._hass, self._automatic_stop(self._cook_id), "jelly_anova automatic stop"
        )

    async def _automatic_stop(self, cook_id: str) -> None:
        if self._other_cook_running(cook_id):
            if self._cook_id == cook_id:
                self._forget_cook()
            return
        try:
            await self._deliver_stop()
        except HomeAssistantError:
            # Stays saved, so it goes out again. The oven may be heating: this one must be seen.
            _LOGGER.warning("The oven could not be told to stop; trying again")
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                ISSUE_STOP,
                is_fixable=False,
                severity=ir.IssueSeverity.CRITICAL,
                translation_key=ISSUE_STOP,
            )
            return
        self._stop_delivered(cook_id)

    def _stop_delivered(self, cook_id: str | None) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, ISSUE_STOP)
        # With a cook still showing, the end is seen in a report, or the stop goes out again.
        # With nothing showing it, the oven's answer is the last word.
        if cook_id == self._cook_id and cook_id is not None:
            if not (self._fresh(self.state) and self._cook_reported(self.state)):
                self._forget_cook()

    async def _deliver_stop(self) -> None:
        """Get a stop to the oven, or raise a plain error. State never decides whether it is sent."""
        for _ in range(2):  # a send that timed out may have arrived, so once more is harmless
            try:
                await self.connection.send("CMD_APO_STOP", None)
            except OvenRefused:
                if self._fresh_idle():  # nothing to stop
                    return
            except OvenError as err:
                _LOGGER.debug("Stop not delivered: %s", type(err).__name__)
            else:
                return
        token = self._entry.data.get("personal_token")
        cooker_id = self.connection.cooker_id or self._cooker_id
        if token and cooker_id:
            try:
                await backup_stop(self._session, token, cooker_id)
            except OvenRefused:
                if self._fresh_idle():
                    return
            except OvenError as err:
                _LOGGER.debug("Backup stop not delivered: %s", type(err).__name__)
            else:
                return
        raise HomeAssistantError("The oven could not be told to stop. Please check it yourself.")

    # --- actions -------------------------------------------------------------

    async def stop(self, cook_id: str | None = None) -> None:
        """Stop the oven. With a cook_id, do nothing if a fresh report shows a different cook."""
        if cook_id is not None and self._other_cook_running(cook_id):
            return
        self._stop_presses += 1
        await self._deliver_stop()
        self._stop_delivered(cook_id or self._cook_id)

    async def start(self, req: CookRequest) -> str:
        """Start a cook, ending any cook running first. Returns its cook id."""
        if req.max_minutes is not None:
            _check_max_minutes(req.max_minutes)
        payload, cook_id = build_start(req, self._cooker())
        return await self._start(payload, cook_id, limit_minutes(req))

    async def start_custom(self, stages: list[dict], max_minutes: int | None) -> str:
        """Start a cook from raw stages. Their timers are not relied on: the limit ends it."""
        if max_minutes is not None:
            _check_max_minutes(max_minutes)
        cook_id = f"ios-{uuid.uuid4()}"
        payload = _custom_payload(stages, cook_id, self._cooker())
        return await self._start(payload, cook_id, int(max_minutes or DEFAULT_MAX_MINUTES))

    async def update(self, changes: dict) -> None:
        """Change the active stage of the running cook."""
        state = self.state
        if not self._fresh(state) or state.mode == "idle" or state.cook is None or not state.cook.active_stage:
            raise HomeAssistantError("No cook is running to change.")
        if state.cook.in_end_stage:
            raise HomeAssistantError("This cook has finished, so it can no longer be changed.")
        stage = updated_stage(state.cook.active_stage, changes)
        try:
            await self.connection.send("CMD_APO_UPDATE_COOK_STAGE", stage)
        except OvenRefused:
            raise HomeAssistantError("The oven refused the change.") from None
        except OvenError:
            raise HomeAssistantError("The oven did not confirm the change.") from None

    def _cooker(self) -> str:
        cooker_id = self.connection.cooker_id
        if cooker_id is None:
            raise HomeAssistantError("The oven is not connected yet. Try again in a minute.")
        return cooker_id

    async def _start(self, payload: dict, cook_id: str, minutes: int) -> str:
        stop_at = dt_util.utcnow() + timedelta(minutes=minutes)
        presses = self._stop_presses
        async with self._start_lock:
            if self._stop_task is not None and not self._stop_task.done():
                await asyncio.wait([self._stop_task])
            await self._make_idle()
            if presses != self._stop_presses:
                raise HomeAssistantError("Stop was pressed, so the new cook was not started.")
            # Saved before it is sent: reports can beat the reply, and a failed send may have
            # arrived. A cook must not be able to run without its limit.
            self._cook_id = cook_id
            self._stop_at = stop_at
            self._timer_seen = False
            self._next_stop_try = None
            self._hold_until = None
            await self._store.async_save(self._data())
            try:
                await self.connection.send("CMD_APO_START", payload)
            except OvenRefused:
                if self._cook_id == cook_id:
                    self._forget_cook()
                raise HomeAssistantError("The oven refused the cook.") from None
            except OvenError:
                raise HomeAssistantError(
                    "The oven did not confirm the start. Please check it: if it did start, "
                    "its stop time still applies."
                ) from None
        return cook_id

    async def _make_idle(self) -> None:
        """Return once a fresh report shows the oven idle, stopping a cook if there is one.

        Needed because the oven answers ok to a start during a cook and ignores it. With no
        fresh report nothing is known, so nothing is stopped and nothing may start.
        """
        state = await self._wait_for(self._fresh, REPORT_WAIT)
        if state is None:
            raise HomeAssistantError("The oven is not reporting, so a cook cannot be started safely.")
        if state.mode == "idle":
            return
        stopped_at = dt_util.utcnow()
        await self._deliver_stop()
        idle = await self._wait_for(
            lambda report: report.mode == "idle" and is_newer(report, stopped_at), IDLE_WAIT
        )
        if idle is None:
            raise HomeAssistantError(
                "The oven did not report that the last cook stopped, so the new cook was not started."
            )

    async def _wait_for(
        self, wanted: Callable[[OvenState], bool], seconds: float
    ) -> OvenState | None:
        """The current or next report that is wanted, or None if none comes in time."""
        if self.state is not None and wanted(self.state):
            return self.state
        waiter = (wanted, self._hass.loop.create_future())
        self._waiters.append(waiter)
        try:
            async with asyncio.timeout(seconds):
                return await waiter[1]
        except TimeoutError:
            return None
        finally:
            self._waiters.remove(waiter)
