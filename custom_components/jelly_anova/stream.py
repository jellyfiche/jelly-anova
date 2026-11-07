"""The oven camera's live stream: one oven stream, shared by every viewer.

Proven on the real oven (2026-11-07): CMD_APO_START_LIVE_STREAM with no payload answers with a
Cloudflare WHEP link in data.webRTCPlayback.url. Each viewer posts its WebRTC offer there. The
start is re-sent every minute while anyone watches, and CMD_APO_STOP_LIVE_STREAM ends it. The
oven streams only during a cook. The link lets anyone watch, so it is never logged or shown.
Protocol credit: awgneo/anova-culinary and ampdot-io/anova-oven-mcp.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from yarl import URL

from .connection import OvenError

_LOGGER = logging.getLogger(__name__)

START = "CMD_APO_START_LIVE_STREAM"
STOP = "CMD_APO_STOP_LIVE_STREAM"
KEEP_ALIVE = 60  # seconds between starts while anyone watches, as the app does
STOP_DELAY = 60  # the stream outlives its last viewer this long (Thomas, 2026-11-07)
OFFER_RETRY_DELAYS = (0.5, 1, 2)  # a refused offer is posted again after these waits
FIRST_OFFER_WAIT = 3  # a new stream needs a moment before it takes viewers (409 otherwise)
STOP_TIMEOUT = 5  # an unload never waits longer than this for the stop
WHEP_TIMEOUT = 15
STUN_SERVER = "stun:stun.cloudflare.com:3478"
FAILED = "The oven's camera did not start"

_sleep = asyncio.sleep  # one place for tests to replace the waits


class CameraFailed(Exception):
    """The camera could not be shown. The message is plain text for the viewer."""


def _link(reply: Any) -> str | None:
    data = reply.get("data") if isinstance(reply, dict) else None
    playback = data.get("webRTCPlayback") if isinstance(data, dict) else None
    url = playback.get("url") if isinstance(playback, dict) else None
    return url if isinstance(url, str) and url else None


def _resource(link: str, location: str | None) -> str | None:
    """The viewer's session address; a malformed one is ignored (it would only be logged)."""
    if not location:
        return None
    try:
        return str(URL(link).join(URL(location)))
    except ValueError:
        return None


class OvenLiveStream:
    """Starts the oven's stream for the first viewer and stops it after the last."""

    def __init__(self, hass: HomeAssistant, connection: Any, session: aiohttp.ClientSession) -> None:
        self._hass = hass
        self._connection = connection
        self._session = session
        self._link: str | None = None  # set while the oven's stream runs
        self._viewers: dict[str, str | None] = {}  # viewer -> its WHEP resource URL
        self._lock = asyncio.Lock()
        self._cancel_keep_alive: Callable[[], None] | None = None
        self._cancel_stop: Callable[[], None] | None = None
        self._gone: set[str] = set()  # viewers that left while their offer was still out
        self._generation = 0  # moved on by end_all: an older watch must not touch the new state
        self._shut = False

    @property
    def watching(self) -> bool:
        return bool(self._viewers)

    async def watch(self, viewer: str, offer_sdp: str) -> str:
        """Start the stream if needed and return the answer to a viewer's offer."""
        if self._shut:
            raise CameraFailed(FAILED)
        self._gone.discard(viewer)
        self._unschedule_stop()
        generation = self._generation
        try:
            async with self._lock:
                if generation != self._generation:
                    raise CameraFailed(FAILED)
                if self._link is None:
                    link = await self._start()
                    if generation != self._generation:
                        raise CameraFailed(FAILED)  # the cook or connection ended meanwhile
                    self._link = link
                    _LOGGER.debug("Live video started")
                    await _sleep(FIRST_OFFER_WAIT)
                elif self._cancel_keep_alive is None:
                    # Back within the grace period: keep-alives had paused, so refresh now.
                    await self._refresh()
                link = self._link
            if link is None or generation != self._generation:
                raise CameraFailed(FAILED)
            answer, resource = await self._offer(link, offer_sdp)
            if viewer in self._gone or generation != self._generation or self._shut:
                # The viewer left, or the stream ended, while the offer was out.
                self._gone.discard(viewer)
                await self._delete(resource)
                raise CameraFailed(FAILED)
        except BaseException:
            if not self._viewers and generation == self._generation and not self._shut:
                self._schedule_stop()  # nobody watches what the oven may have started
            raise
        self._viewers[viewer] = resource
        if self._cancel_keep_alive is None:
            self._cancel_keep_alive = async_track_time_interval(
                self._hass, self._keep_alive, timedelta(seconds=KEEP_ALIVE), name="jelly_anova camera keep-alive"
            )
        return answer

    async def leave(self, viewer: str) -> None:
        """End a viewer's session; the stream stops a while after the last one leaves."""
        if viewer not in self._viewers:
            self._gone.add(viewer)  # its offer may still be out: watch() drops it on return
            return
        await self._delete(self._viewers.pop(viewer))
        if not self._viewers:
            self._stop_keep_alive()
            self._schedule_stop()

    @callback
    def end_all(self) -> None:
        """The cook or the connection ended: the stream is gone, nothing to send."""
        self._generation += 1
        self._viewers.clear()
        self._gone.clear()
        self._stop_keep_alive()
        self._unschedule_stop()
        self._link = None

    async def shutdown(self) -> None:
        """Unloading: stop a running stream, without waiting long."""
        if self._link is not None:
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    await self._connection.send(STOP, None, timeout=STOP_TIMEOUT)
                _LOGGER.debug("Live video stop sent")
            except Exception:  # noqa: BLE001 - the stream ends by itself without keep-alives
                _LOGGER.debug("Live video stop not confirmed on unload")
        self._shut = True
        self.end_all()

    # --- inside ----------------------------------------------------------------

    async def _start(self) -> str:
        try:
            reply = await self._connection.send(START, None)
        except OvenError as err:
            _LOGGER.debug("Live video start refused: %s", type(err).__name__)
            raise CameraFailed(FAILED) from None
        link = _link(reply)
        if link is None:
            _LOGGER.debug("Live video start answered without a video link")
            raise CameraFailed(FAILED)
        return link

    async def _refresh(self) -> None:
        try:
            link = _link(await self._connection.send(START, None))
        except OvenError as err:
            _LOGGER.debug("Live video keep-alive failed: %s", type(err).__name__)
            return
        if link is not None:
            self._link = link

    async def _keep_alive(self, _now: datetime) -> None:
        if self._viewers:
            await self._refresh()

    async def _offer(self, link: str, offer_sdp: str) -> tuple[str, str | None]:
        """Post the offer (WHEP), retrying a refusal as the app does."""
        for attempt in range(len(OFFER_RETRY_DELAYS) + 1):
            if attempt:
                await _sleep(OFFER_RETRY_DELAYS[attempt - 1])
            try:
                async with self._session.post(
                    link,
                    data=offer_sdp,
                    headers={"Content-Type": "application/sdp"},
                    timeout=aiohttp.ClientTimeout(total=WHEP_TIMEOUT),
                ) as response:
                    status = response.status
                    answer = await response.text()
                    location = response.headers.get("Location")
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Camera offer not delivered: %s", type(err).__name__)
                continue
            if status in (200, 201) and answer.strip():
                return answer, _resource(link, location)
            _LOGGER.debug("Camera offer refused with %s, attempt %d", status, attempt + 1)
        raise CameraFailed(FAILED)

    async def _delete(self, resource: str | None) -> None:
        """End one viewer's session at the video service."""
        if not resource:
            return
        try:
            async with self._session.delete(resource, timeout=aiohttp.ClientTimeout(total=WHEP_TIMEOUT)):
                pass
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Could not end a camera viewer's session")

    def _stop_keep_alive(self) -> None:
        if self._cancel_keep_alive is not None:
            self._cancel_keep_alive()
            self._cancel_keep_alive = None

    def _schedule_stop(self) -> None:
        self._unschedule_stop()
        self._cancel_stop = async_call_later(self._hass, STOP_DELAY, self._stop_later)

    def _unschedule_stop(self) -> None:
        if self._cancel_stop is not None:
            self._cancel_stop()
            self._cancel_stop = None

    async def _stop_later(self, _now: datetime) -> None:
        self._cancel_stop = None
        if self._viewers or self._link is None:
            return
        self._link = None
        try:
            await self._connection.send(STOP, None)
            _LOGGER.debug("Live video stop sent")
        except OvenError as err:
            _LOGGER.debug("Live video stop failed: %s", type(err).__name__)
