#!/usr/bin/env python3
"""Minimal Amazon IVS Real-Time audio publisher and subscriber validation."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import av
import numpy as np
import requests
from aiortc import (
    AudioStreamTrack,
    RTCBundlePolicy,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)

WHIP_URL = "https://global.whip.live-video.net"


def require_ivs_url(value: str) -> str:
    """Return a trusted IVS HTTPS endpoint or reject it before token forwarding."""

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise RuntimeError("IVS SDP endpoint used an untrusted destination") from error
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".live-video.net")
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise RuntimeError("IVS SDP endpoint used an untrusted destination")
    return value


def safe_ivs_redirect(current_url: str, location: str) -> str:
    """Resolve an IVS redirect without forwarding tokens to another host."""

    return require_ivs_url(urljoin(current_url, location))


def load_participant(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text())
    participant = payload["participantToken"]
    return participant["token"], participant["participantId"]


def parse_token(token: str) -> dict[str, Any]:
    encoded = token.split(".")[1]
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded))


def post_sdp(url: str, token: str, offer: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/sdp",
    }
    current_url = require_ivs_url(url)
    for _ in range(6):
        response = requests.post(
            current_url,
            data=offer,
            headers=headers,
            allow_redirects=False,
            timeout=15,
        )
        if response.status_code == 201:
            return response.text
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise RuntimeError("IVS SDP redirect omitted Location")
            current_url = safe_ivs_redirect(current_url, location)
            continue
        raise RuntimeError(f"IVS SDP exchange failed: HTTP {response.status_code}")
    raise RuntimeError("IVS SDP exchange exceeded the redirect limit")


def fix_ivs_answer_sdp(sdp: str) -> str:
    """Copy bundled ICE candidates into each media section for aiortc."""

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


def peer_connection() -> RTCPeerConnection:
    config = RTCConfiguration()
    config.bundlePolicy = RTCBundlePolicy.MAX_BUNDLE
    return RTCPeerConnection(config)


async def wait_connected(pc: RTCPeerConnection, timeout: float = 15) -> None:
    async def poll() -> None:
        while pc.connectionState not in {"connected", "completed"}:
            if pc.connectionState in {"closed", "failed"}:
                raise RuntimeError(f"Peer connection entered {pc.connectionState}")
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


class ToneAudioTrack(AudioStreamTrack):
    """Publish a deterministic 440 Hz marker as 20 ms, 48 kHz PCM frames."""

    kind = "audio"

    def __init__(self, sample_rate: int = 48_000, frequency: int = 440) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.frequency = frequency
        self.samples_per_frame = sample_rate // 50
        self.position = 0

    async def recv(self) -> av.AudioFrame:
        start = self.position
        indexes = np.arange(start, start + self.samples_per_frame)
        wave = np.sin(2 * math.pi * self.frequency * indexes / self.sample_rate)
        samples = (wave * 8_000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = self.sample_rate
        frame.pts = self.position
        frame.time_base = Fraction(1, self.sample_rate)
        self.position += self.samples_per_frame
        await asyncio.sleep(self.samples_per_frame / self.sample_rate)
        return frame


class BlankVideoTrack(VideoStreamTrack):
    """Publish blank H.264-compatible video frames required by IVS WHIP."""

    kind = "video"

    def __init__(self, width: int = 320, height: int = 180) -> None:
        super().__init__()
        self.width = width
        self.height = height

    async def recv(self) -> av.VideoFrame:
        pts, time_base = await self.next_timestamp()
        frame = av.VideoFrame(self.width, self.height, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = pts
        frame.time_base = time_base
        return frame


class BufferedAudioTrack(AudioStreamTrack):
    """Publish queued 16-bit mono PCM and clear queued speech on interruption."""

    kind = "audio"

    def __init__(self, sample_rate: int = 24_000) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.samples_per_frame = sample_rate // 50
        self.bytes_per_frame = self.samples_per_frame * 2
        self.position = 0
        self._buffer = bytearray()
        self._lock = asyncio.Lock()

    async def add_audio(self, pcm: bytes) -> None:
        async with self._lock:
            self._buffer.extend(pcm)

    async def interrupt(self) -> int:
        async with self._lock:
            cleared = len(self._buffer)
            self._buffer.clear()
            return cleared

    async def buffered_bytes(self) -> int:
        async with self._lock:
            return len(self._buffer)

    async def recv(self) -> av.AudioFrame:
        async with self._lock:
            chunk = bytes(self._buffer[: self.bytes_per_frame])
            del self._buffer[: self.bytes_per_frame]
        chunk += bytes(self.bytes_per_frame - len(chunk))
        samples = np.frombuffer(chunk, dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = self.sample_rate
        frame.pts = self.position
        frame.time_base = Fraction(1, self.sample_rate)
        self.position += self.samples_per_frame
        await asyncio.sleep(self.samples_per_frame / self.sample_rate)
        return frame


async def publish(token: str, track: AudioStreamTrack) -> RTCPeerConnection:
    pc = peer_connection()
    pc.addTransceiver(track, direction="sendrecv")
    pc.addTransceiver(BlankVideoTrack(), direction="sendrecv")
    await pc.setLocalDescription(await pc.createOffer())
    answer = await asyncio.to_thread(
        post_sdp,
        WHIP_URL,
        token,
        pc.localDescription.sdp,
    )
    await pc.setRemoteDescription(RTCSessionDescription(fix_ivs_answer_sdp(answer), "answer"))
    await wait_connected(pc)
    return pc


@dataclass
class AudioObservation:
    frames: int
    samples: int
    nonzero_frames: int
    peak_rms: float
    input_sample_rates: list[int]
    connection_state: str


async def observe_audio(
    token: str,
    participant_id: str,
    frame_limit: int = 100,
) -> AudioObservation:
    pc, track = await subscribe_audio(token, participant_id)
    return await observe_track(track, pc, frame_limit)


async def subscribe_audio(
    token: str,
    participant_id: str,
) -> tuple[RTCPeerConnection, Any]:
    """Subscribe to one IVS participant and return its audio track."""

    pc = peer_connection()
    pc.addTransceiver("audio", direction="recvonly")
    loop = asyncio.get_running_loop()
    track_future: asyncio.Future[Any] = loop.create_future()

    @pc.on("track")
    def on_track(track: Any) -> None:
        if track.kind == "audio" and not track_future.done():
            track_future.set_result(track)

    await pc.setLocalDescription(await pc.createOffer())
    token_payload = parse_token(token)
    whip_url = require_ivs_url(str(token_payload["whip_url"]))
    whep_url = f"{whip_url.rstrip('/')}/subscribe/{quote(participant_id, safe='')}"
    answer = await asyncio.to_thread(
        post_sdp,
        whep_url,
        token,
        pc.localDescription.sdp,
    )
    await pc.setRemoteDescription(RTCSessionDescription(fix_ivs_answer_sdp(answer), "answer"))
    await wait_connected(pc)
    track = await asyncio.wait_for(track_future, 10)
    return pc, track


async def observe_track(
    track: Any,
    pc: RTCPeerConnection,
    frame_limit: int = 100,
) -> AudioObservation:
    """Measure audio received from an already connected aiortc track."""

    samples_seen = 0
    nonzero_frames = 0
    peak_rms = 0.0
    sample_rates: set[int] = set()

    try:
        for _ in range(frame_limit):
            frame = await asyncio.wait_for(track.recv(), 3)
            values = frame.to_ndarray().astype(np.float32).reshape(-1)
            samples_seen += values.size
            sample_rates.add(frame.sample_rate)
            rms = float(np.sqrt(np.mean(values * values))) if values.size else 0.0
            peak_rms = max(peak_rms, rms)
            if rms > 100:
                nonzero_frames += 1
        return AudioObservation(
            frames=frame_limit,
            samples=samples_seen,
            nonzero_frames=nonzero_frames,
            peak_rms=round(peak_rms, 2),
            input_sample_rates=sorted(sample_rates),
            connection_state=pc.connectionState,
        )
    finally:
        await pc.close()


async def run_loopback(
    publisher_file: Path,
    subscriber_file: Path,
    frame_limit: int,
) -> AudioObservation:
    publisher_token, publisher_id = load_participant(publisher_file)
    subscriber_token, _ = load_participant(subscriber_file)
    publisher = await publish(publisher_token, ToneAudioTrack())
    try:
        await asyncio.sleep(0.5)
        return await observe_audio(subscriber_token, publisher_id, frame_limit)
    finally:
        await publisher.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publisher-token-file", type=Path, required=True)
    parser.add_argument("--subscriber-token-file", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=100)
    args = parser.parse_args()

    observation = asyncio.run(
        run_loopback(
            args.publisher_token_file,
            args.subscriber_token_file,
            args.frames,
        )
    )
    print(json.dumps(asdict(observation), indent=2))
    if observation.nonzero_frames < max(1, args.frames // 2):
        raise SystemExit("Expected a non-silent IVS audio stream")


if __name__ == "__main__":
    main()
