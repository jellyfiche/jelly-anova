"""Holds the Anova websocket open, reconnects, matches replies and sends commands.

Knows nothing about entities. Never logs or puts in an error message a URL, token or the text of
a connection error: only the exception type name is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from .auth import AnovaAuth, AuthFailed, CannotConnect, InvalidApiKey
from .state import OvenState, is_newer, parse_state

_LOGGER = logging.getLogger(__name__)

HOST = "wss://devices.anovaculinary.io"
SUBPROTOCOL = "ANOVA_V2"
HEARTBEAT = 20
FIRST_DELAY = 5
MAX_DELAY = 300
BACKUP_WAIT = 8  # seconds the backup stop waits for the oven's reply
CONNECT_TIMEOUT = 15  # seconds allowed to fetch a token and open a websocket

# Failures that mean "try again later". Their text is never logged.
_RETRY_ERRORS = (CannotConnect, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError)
_AUTH_ERRORS = (AuthFailed, InvalidApiKey)
_CLOSED_TYPES = (
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.ERROR,
)


class OvenError(Exception):
    """Base of the errors from talking to the oven."""


class OvenRefused(OvenError):
    """The oven answered with an error."""


class OvenNoReply(OvenError):
    """The oven did not answer in time."""


class OvenOffline(OvenError):
    """No connection to the oven's cloud, and one reconnect try failed."""


def _frame(command: str, cooker_id: str, payload: dict | None) -> tuple[str, str]:
    """Build the text of a command message; return (request id, text)."""
    body: dict[str, Any] = {"id": cooker_id, "type": command}
    if payload is not None:
        body["payload"] = payload
    request_id = str(uuid.uuid4())
    text = json.dumps({"command": command, "requestId": request_id, "payload": body})
    return request_id, text


def _check(reply: Any) -> dict:
    """Return a RESPONSE payload, or raise OvenRefused for an error status."""
    reply = reply if isinstance(reply, dict) else {}
    if reply.get("status") != "ok":
        error = reply.get("error")
        raise OvenRefused(error if isinstance(error, str) else "The oven refused the command")
    return reply


class OvenConnection:
    """One websocket to the oven's cloud, kept open for as long as run() is going."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: AnovaAuth,
        on_state: Callable[[OvenState], None],
        on_status: Callable[[bool], None],
        on_auth_failed: Callable[[], None] | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._auth = auth
        self._on_state = on_state
        self._on_status = on_status
        self._on_auth_failed = on_auth_failed
        self._sleep = sleep
        self.cooker_id: str | None = None
        self._cooker_known = asyncio.Event()
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, tuple[Any, asyncio.Future]] = {}
        self._connecting: asyncio.Future | None = None
        self._closing = False
        self._auth_reported = False
        self._delay = FIRST_DELAY
        self._sleeper: asyncio.Future | None = None
        self._connected_at = datetime.min.replace(tzinfo=UTC)
        self._state: OvenState | None = None
        self._up = False

    # --- opening and running -------------------------------------------------

    async def _connect(self) -> Any:
        """Fetch a fresh token and open the websocket (one bounded step)."""
        token = await self._auth.access_token()
        url = f"{HOST}?token={quote(token, safe='')}&supportedAccessories=APO&platform=ios"
        async with asyncio.timeout(CONNECT_TIMEOUT):
            return await self._session.ws_connect(
                url,
                protocols=(SUBPROTOCOL,),
                heartbeat=HEARTBEAT,
                timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT),
            )

    async def _open(self) -> None:
        """Open the websocket unless it is already open. Raises on failure."""
        async with self._lock:
            if self._closing:
                raise OvenOffline("Closed")
            if self._ws is not None and not self._ws.closed:
                return
            task = asyncio.ensure_future(self._connect())
            self._connecting = task
            try:
                ws = await task
            except asyncio.CancelledError:
                # close() cancels a connect in flight; a cancel of this caller passes on.
                current = asyncio.current_task()
                if self._closing and current is not None and current.cancelling() == 0:
                    raise OvenOffline("Closed") from None
                raise
            finally:
                self._connecting = None
            if self._closing:  # close() ran while the connect was finishing
                with contextlib.suppress(Exception):
                    await ws.close()
                raise OvenOffline("Closed")
            if self._ws is not None:  # a closed socket whose reader has not noticed yet
                if self._reader is not None:
                    self._reader.cancel()
                self._drop(self._ws)
            self._ws = ws
            self._connected_at = datetime.now(UTC)
            self._state = None
            self._reader = asyncio.create_task(self._read(ws))
            self._set_up(True)

    def _safe(self, callback: Callable | None, *args: Any) -> None:
        """Call a callback; a failing one is logged by type name and never stops us."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("A callback failed: %s", type(err).__name__)

    def _set_up(self, up: bool) -> None:
        if up != self._up:
            self._up = up
            self._safe(self._on_status, up)

    def _auth_failed(self) -> None:
        if not self._auth_reported:
            self._auth_reported = True
            _LOGGER.warning("Anova refused the login; signing in again is needed")
            self._safe(self._on_auth_failed)

    async def run(self) -> None:
        """Stay connected until close(), or until the login is refused."""
        while not self._closing and not self._auth_reported:
            try:
                await self._open()
            except _AUTH_ERRORS:
                self._auth_failed()
                return
            except OvenOffline:
                if self._closing:
                    return
                await self._backoff()
                continue
            except _RETRY_ERRORS as err:
                _LOGGER.debug("Could not connect to Anova: %s", type(err).__name__)
                await self._backoff()
                continue
            except Exception as err:  # noqa: BLE001 - run() must not die silently
                _LOGGER.warning("Unexpected error connecting to Anova: %s", type(err).__name__)
                await self._backoff()
                continue
            if self._reader is not None:
                await asyncio.wait([self._reader])
            if self._closing:
                return
            await self._backoff()

    async def _backoff(self) -> None:
        delay = self._delay
        self._delay = min(delay * 2, MAX_DELAY)
        self._sleeper = asyncio.ensure_future(self._sleep(delay))
        try:
            await self._sleeper
        except asyncio.CancelledError:
            if not self._closing:
                raise

    async def close(self) -> None:
        """Stop run() and close the websocket. Does not wait for a connect in flight."""
        self._closing = True
        if self._sleeper is not None:
            self._sleeper.cancel()
        if self._connecting is not None:
            self._connecting.cancel()
        reader, ws = self._reader, self._ws
        if reader is not None:
            reader.cancel()
            await asyncio.wait([reader])
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        if ws is not None:
            self._drop(ws)

    # --- reading -------------------------------------------------------------

    async def _read(self, ws: Any) -> None:
        try:
            while True:
                message = await ws.receive()
                if message.type in _CLOSED_TYPES:
                    break
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._handle(message.data)
                    except Exception as err:  # noqa: BLE001 - one bad message is not the end
                        _LOGGER.debug("Could not handle a message: %s", type(err).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Anova connection dropped: %s", type(err).__name__)
        finally:
            self._drop(ws)
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()

    def _drop(self, ws: Any) -> None:
        """Fail the commands waiting on this ws; mark the connection down if it is the current one."""
        for request_id, (owner, future) in list(self._pending.items()):
            if owner is ws:
                del self._pending[request_id]
                if not future.done():
                    future.set_exception(OvenOffline("Connection lost"))
        if ws is self._ws:
            self._ws = None
            self._set_up(False)

    def _handle(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        command = data.get("command")
        payload = data.get("payload")
        if command == "EVENT_APO_WIFI_LIST":
            self._handle_list(payload)
        elif command == "EVENT_APO_STATE":
            self._handle_state(payload)
        elif command == "RESPONSE":
            entry = self._pending.pop(str(data.get("requestId")), None)
            if entry is not None and not entry[1].done():
                entry[1].set_result(payload)

    def _handle_list(self, payload: Any) -> None:
        first = payload[0] if isinstance(payload, list) and payload else None
        cooker = first.get("cookerId") if isinstance(first, dict) else None
        if isinstance(cooker, str) and cooker:
            self.cooker_id = cooker
            self._cooker_known.set()

    def _handle_state(self, payload: Any) -> None:
        try:
            state = parse_state(payload, self._state)
        except Exception as err:  # noqa: BLE001 - a bad report must not end the connection
            _LOGGER.debug("Could not read an Anova report: %s", type(err).__name__)
            return
        # Live means stamped after we connected (so a cached copy is dropped), and after the
        # last report we kept.
        floor = self._state.updated if self._state else self._connected_at
        if not is_newer(state, floor):
            return
        self._state = state
        self._delay = FIRST_DELAY  # a live report proves the connection is healthy
        self._safe(self._on_state, state)

    # --- sending -------------------------------------------------------------

    async def _reopen(self) -> None:
        """If the connection is down, try once to open it. Raises OvenOffline if it stays down."""
        if self._auth_reported:
            raise OvenOffline("Login refused")
        if self._ws is None or self._ws.closed:
            try:
                await self._open()
            except _AUTH_ERRORS:
                self._auth_failed()
                raise OvenOffline("Login refused") from None
            except OvenError:
                raise
            except _RETRY_ERRORS as err:
                _LOGGER.debug("Reconnect for a command failed: %s", type(err).__name__)
                raise OvenOffline("Cannot reach the oven's cloud") from None

    async def ensure_open(self, timeout: float = 10) -> None:
        """Try once to open a connection that is down, for an action that needs reports."""
        try:
            async with asyncio.timeout(timeout):
                await self._reopen()
        except TimeoutError:
            raise OvenOffline("Cannot reach the oven's cloud") from None

    async def send(self, command: str, payload: dict | None, timeout: float = 10) -> dict:
        """Send a command and return the oven's reply payload.

        If the connection is down, one reconnect is tried first. The whole call, reconnect
        included, is limited to `timeout`: OvenOffline if it ran out before the command was
        written, OvenNoReply if after.
        """
        written = False
        try:
            async with asyncio.timeout(timeout):
                await self._reopen()
                if self.cooker_id is None:
                    await self._cooker_known.wait()
                ws = self._ws
                if ws is None:
                    raise OvenOffline("Connection lost")
                request_id, text = _frame(command, self.cooker_id, payload)
                future: asyncio.Future = asyncio.get_running_loop().create_future()
                self._pending[request_id] = (ws, future)
                try:
                    written = True
                    await ws.send_str(text)
                    reply = await future
                finally:
                    self._pending.pop(request_id, None)
        except TimeoutError:
            if written:
                raise OvenNoReply("The oven did not answer") from None
            raise OvenOffline("Cannot reach the oven's cloud") from None
        except OvenError:
            raise
        except _RETRY_ERRORS as err:
            _LOGGER.debug("Could not send a command: %s", type(err).__name__)
            raise OvenOffline("Connection lost") from None
        return _check(reply)


async def backup_stop(session: aiohttp.ClientSession, personal_token: str, cooker_id: str) -> None:
    """Stop the oven over a separate connection that uses the personal token.

    Proven on the real oven: the personal token works with no subprotocol. The connect is
    limited to CONNECT_TIMEOUT, the write and the wait for the reply to BACKUP_WAIT.
    """
    url = f"{HOST}?token={quote(personal_token, safe='')}&supportedAccessories=APO"
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            ws = await session.ws_connect(
                url, timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT)
            )
    except _RETRY_ERRORS as err:
        _LOGGER.debug("Backup stop could not connect: %s", type(err).__name__)
        raise OvenOffline("Cannot reach the oven's cloud") from None
    try:
        request_id, text = _frame("CMD_APO_STOP", cooker_id, None)
        try:
            async with asyncio.timeout(BACKUP_WAIT):
                await ws.send_str(text)
                while True:
                    message = await ws.receive()
                    if message.type in _CLOSED_TYPES:
                        raise OvenOffline("Connection lost")
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        continue
                    if (
                        isinstance(data, dict)
                        and data.get("command") == "RESPONSE"
                        and data.get("requestId") == request_id
                    ):
                        _check(data.get("payload"))
                        return
        except TimeoutError:
            raise OvenNoReply("The oven did not answer") from None
    except OvenError:
        raise
    except _RETRY_ERRORS as err:
        _LOGGER.debug("Backup stop failed: %s", type(err).__name__)
        raise OvenOffline("Connection lost") from None
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
