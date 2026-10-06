"""Minimal WHIP/WHEP signalling for an existing Amazon IVS Real-Time stage."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests
from aiortc import (
    RTCBundlePolicy,
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)

from .adapters import IVSBlankVideoTrack, IVSInputStream, IVSOutputAudioTrack

_PUBLISH_URL = "https://global.whip.live-video.net"
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class IVSSignallingError(RuntimeError):
    """WHIP/WHEP signalling failed."""


def require_ivs_url(value: str) -> str:
    """Return a trusted IVS HTTPS endpoint or reject it before token forwarding."""

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise IVSSignallingError("IVS signalling refused an untrusted endpoint") from error
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".live-video.net")
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise IVSSignallingError("IVS signalling refused an untrusted endpoint")
    return value


def parse_stage_token(token: str) -> dict[str, Any]:
    """Decode an IVS participant token payload without logging or verifying it."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("IVS participant token is not a JWT")

    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        value = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("IVS participant token payload is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("IVS participant token payload must be an object")  # noqa: TRY004
    return value


def require_stage_capabilities(payload: dict[str, Any]) -> None:
    """Require publish and subscribe capabilities from an IVS token payload."""
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("IVS participant token has no capabilities object")  # noqa: TRY004
    missing = [
        capability
        for capability in ("publish", "subscribe")
        if capabilities.get(f"allow_{capability}") is not True
    ]
    if missing:
        raise ValueError(f"IVS participant token lacks capabilities: {', '.join(missing)}")


def fix_ivs_answer_sdp(sdp: str) -> str:
    """Copy bundled ICE candidates into each media section that lacks them."""
    lines = sdp.splitlines()
    candidates = [line for line in lines if line.startswith("a=candidate:")]
    if not candidates:
        return sdp

    fixed: list[str] = []
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        if section[0].startswith("m=") and not any(
            line.startswith("a=candidate:") for line in section
        ):
            section.extend(candidates)
            section.append("a=end-of-candidates")
        fixed.extend(section)
        section.clear()

    for line in lines:
        if line.startswith("m="):
            flush()
        section.append(line)
    flush()
    return "\r\n".join(fixed) + "\r\n"


def _safe_redirect(current_url: str, location: str) -> str:
    return require_ivs_url(urljoin(current_url, location))


def _post_sdp_sync(url: str, token: str, offer: str, max_redirects: int) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }
    current_url = require_ivs_url(url)

    for _ in range(max_redirects + 1):
        response = requests.post(
            current_url,
            data=offer,
            headers=headers,
            allow_redirects=False,
            timeout=10,
        )
        if response.status_code in _REDIRECT_CODES:
            location = response.headers.get("Location")
            if not location:
                raise IVSSignallingError("IVS signalling redirect omitted Location")
            current_url = _safe_redirect(current_url, location)
            continue
        if response.status_code != 201:
            raise IVSSignallingError(f"IVS signalling returned HTTP {response.status_code}")
        return response.text

    raise IVSSignallingError("IVS signalling exceeded its redirect limit")


async def post_sdp(
    url: str,
    token: str,
    offer: str,
    *,
    max_redirects: int = 5,
) -> str:
    """POST an SDP offer without blocking the asyncio event loop."""
    return await asyncio.to_thread(_post_sdp_sync, url, token, offer, max_redirects)


def _peer_configuration() -> RTCConfiguration:
    configuration = RTCConfiguration()
    configuration.bundlePolicy = RTCBundlePolicy.MAX_BUNDLE
    return configuration


def _watch_connection(
    pc: RTCPeerConnection,
    on_disconnect: Callable[[], None],
) -> None:
    @pc.on("connectionstatechange")
    def connection_state_changed() -> None:
        if pc.connectionState in {"closed", "disconnected", "failed"}:
            on_disconnect()


def configure_ivs_publisher_media(
    pc: RTCPeerConnection,
    audio_track: IVSOutputAudioTrack,
) -> IVSBlankVideoTrack:
    """Add the audio and H.264 blank-video transceivers required by IVS WHIP."""
    pc.addTransceiver(audio_track, direction="sendrecv")

    video_track = IVSBlankVideoTrack()
    video_transceiver = pc.addTransceiver(video_track, direction="sendrecv")
    h264_codecs = [
        codec
        for codec in RTCRtpSender.getCapabilities("video").codecs
        if codec.mimeType.lower() == "video/h264"
    ]
    if not h264_codecs:
        raise RuntimeError("aiortc has no H.264 video codec capability")
    video_transceiver.setCodecPreferences(h264_codecs)
    return video_track


async def join_as_audio_publisher(
    token: str,
    audio_track: IVSOutputAudioTrack,
    *,
    on_disconnect: Callable[[], None],
) -> RTCPeerConnection:
    """Join an existing IVS stage with audio and required blank H.264 video."""
    pc = RTCPeerConnection(_peer_configuration())
    _watch_connection(pc, on_disconnect)
    configure_ivs_publisher_media(pc, audio_track)

    try:
        await pc.setLocalDescription(await pc.createOffer())
        answer = await post_sdp(_PUBLISH_URL, token, pc.localDescription.sdp)
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=fix_ivs_answer_sdp(answer), type="answer")
        )
        return pc
    except BaseException:
        await pc.close()
        raise


async def join_as_audio_subscriber(
    token: str,
    payload: dict[str, Any],
    participant_id: str,
    input_stream: IVSInputStream,
    *,
    on_disconnect: Callable[[], None],
) -> RTCPeerConnection:
    """Subscribe to one existing IVS participant and attach its audio track."""
    whip_url = payload.get("whip_url")
    if not isinstance(whip_url, str):
        raise ValueError("IVS participant token has no valid HTTPS whip_url")
    whip_url = require_ivs_url(whip_url)

    pc = RTCPeerConnection(_peer_configuration())
    _watch_connection(pc, on_disconnect)
    pc.addTransceiver("audio", direction="recvonly")

    @pc.on("track")
    def track_received(track: Any) -> None:
        if track.kind == "audio":
            input_stream.attach_track(track)

    whep_url = f"{whip_url.rstrip('/')}/subscribe/{quote(participant_id, safe='')}"
    try:
        await pc.setLocalDescription(await pc.createOffer())
        answer = await post_sdp(whep_url, token, pc.localDescription.sdp)
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=fix_ivs_answer_sdp(answer), type="answer")
        )
        return pc
    except BaseException:
        await pc.close()
        raise
