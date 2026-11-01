"""Turn the integration's errors into plain Home Assistant errors, never a traceback or a token."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .connection import OvenNoReply, OvenOffline, OvenRefused
from .cook import CookError


@contextmanager
def plain_errors() -> Iterator[None]:
    """A request the oven would refuse is a ServiceValidationError with its message. A failure to
    reach or hear from the oven is a HomeAssistantError in plain words. The oven's own error text
    and the connection's messages (which can hold a URL) are never passed on."""
    try:
        yield
    except (ServiceValidationError, HomeAssistantError):
        raise
    except CookError as err:
        raise ServiceValidationError(str(err)) from None
    except OvenRefused:
        raise HomeAssistantError("The oven refused the command.") from None
    except OvenNoReply:
        raise HomeAssistantError("The oven did not answer in time. Check it and try again.") from None
    except OvenOffline:
        raise HomeAssistantError("The oven's cloud cannot be reached right now. Try again in a minute.") from None
