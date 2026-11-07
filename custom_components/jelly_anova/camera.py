"""The oven's camera: live video during a cook, played by Home Assistant's own WebRTC support.

Each viewer's offer goes to the oven's video link (stream.py) and the answer comes back; the
video then flows from Anova's video service to the viewer, as in the app. No still pictures.
"""

from __future__ import annotations

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCClientConfiguration,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from webrtc_models import RTCConfiguration, RTCIceCandidateInit, RTCIceServer

from .entity import JellyAnovaEntity
from .stream import STUN_SERVER, CameraFailed

CAMERA = EntityDescription(key="camera", translation_key="camera")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([OvenCamera(entry.runtime_data, entry.entry_id, CAMERA)])


class OvenCamera(JellyAnovaEntity, Camera):
    """Live only while the oven reports a cook; the oven offers no video otherwise."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, runtime, entry_id: str, description: EntityDescription) -> None:
        JellyAnovaEntity.__init__(self, runtime, entry_id, description)
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        state = self.oven_state
        return super().available and state is not None and state.mode == "cook"

    @property
    def entity_picture(self) -> str | None:
        """No still picture, so cards show the icon, not a broken image."""
        return None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return None

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        try:
            answer = await self.runtime.live_stream.watch(session_id, offer_sdp)
        except CameraFailed as err:
            send_message(WebRTCError("camera_failed", str(err)))
            return
        send_message(WebRTCAnswer(answer))

    async def async_on_webrtc_candidate(self, session_id: str, candidate: RTCIceCandidateInit) -> None:
        """The video service's answer carries its own candidates; the viewer's are not needed."""

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        self.hass.async_create_task(self.runtime.live_stream.leave(session_id), "jelly_anova camera leave")

    @callback
    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        return WebRTCClientConfiguration(configuration=RTCConfiguration(ice_servers=[RTCIceServer(urls=STUN_SERVER)]))
