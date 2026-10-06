"""Custom Strands streams and an aiortc output track for Amazon IVS audio."""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Protocol

from aiortc import AudioStreamTrack, VideoStreamTrack
from av import AudioFrame, AudioResampler, VideoFrame
from strands.bidi.models import AudioCapable, AudioStreamConfig
from strands.bidi.types import (
    AudioChannel,
    AudioDelta,
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiConnectionStopEvent,
    BidiOutputEvent,
    InputStream,
    OutputStream,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from strands.bidi import BidiAgent
    from strands.bidi.types import Role


class IVSAudioTrack(Protocol):
    """Subset of an aiortc audio track used by the input adapter."""

    kind: str

    async def recv(self) -> AudioFrame:
        """Receive the next audio frame."""
        ...

    def stop(self) -> object:
        """Stop the track."""
        ...


@dataclass(frozen=True)
class PCMFormat:
    """Signed 16-bit little-endian PCM stream settings."""

    sample_rate: int
    channels: AudioChannel = 1
    frame_duration_ms: int = 20

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")
        if self.frame_duration_ms <= 0:
            raise ValueError("frame_duration_ms must be positive")
        if self.sample_rate * self.frame_duration_ms % 1000:
            raise ValueError("frame duration must contain a whole number of samples")

    @property
    def samples_per_frame(self) -> int:
        """Number of samples per channel in one real-time output frame."""
        return self.sample_rate * self.frame_duration_ms // 1000

    @property
    def bytes_per_frame(self) -> int:
        """Number of bytes in one signed 16-bit PCM output frame."""
        return self.samples_per_frame * self.channels * 2

    def as_strands_config(self) -> AudioStreamConfig:
        """Return the matching Strands audio stream configuration."""
        return AudioStreamConfig(sample_rate=self.sample_rate, channels=self.channels, format="pcm")


DEFAULT_INPUT_FORMAT = PCMFormat(sample_rate=16000)
DEFAULT_OUTPUT_FORMAT = PCMFormat(sample_rate=24000)


def pcm_bytes_from_frame(frame: AudioFrame) -> bytes:
    """Extract tightly packed signed 16-bit PCM bytes from an AV audio frame."""
    if frame.format.name != "s16":
        raise ValueError(f"expected packed signed 16-bit PCM, received {frame.format.name}")

    channel_count = len(frame.layout.channels)
    expected_size = frame.samples * channel_count * 2
    if len(frame.planes) != 1:
        raise ValueError("expected one packed PCM plane")

    plane = bytes(frame.planes[0])
    if len(plane) < expected_size:
        raise ValueError("audio plane is shorter than its declared sample count")
    return plane[:expected_size]


def _validate_model_stream(actual: AudioStreamConfig, expected: PCMFormat, direction: str) -> None:
    expected_tuple = ("pcm", expected.sample_rate, expected.channels)
    actual_tuple = (actual["format"], actual["sample_rate"], actual["channels"])
    if actual_tuple != expected_tuple:
        raise ValueError(
            f"{direction} audio configuration mismatch: "
            f"expected {expected_tuple}, received {actual_tuple}"
        )


class _PCMBuffer:
    """Bounded, non-blocking PCM buffer for an aiortc pull track."""

    def __init__(self, *, max_bytes: int, alignment: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if alignment <= 0:
            raise ValueError("alignment must be positive")

        self._max_bytes = max_bytes
        self._alignment = alignment
        self._data = bytearray()
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Open and clear the buffer."""
        async with self._lock:
            self._data.clear()
            self._closed = False

    async def stop(self) -> None:
        """Close and clear the buffer."""
        async with self._lock:
            self._closed = True
            self._data.clear()

    async def put(self, data: bytes) -> int:
        """Append PCM and return bytes dropped from the oldest buffered audio."""
        if not data:
            return 0
        if len(data) % self._alignment:
            raise ValueError("PCM data is not aligned to complete samples")

        async with self._lock:
            if self._closed:
                return len(data)

            self._data.extend(data)
            overflow = max(0, len(self._data) - self._max_bytes)
            if not overflow:
                return 0

            drop = ((overflow + self._alignment - 1) // self._alignment) * self._alignment
            del self._data[:drop]
            return drop

    async def read(self, byte_count: int) -> bytes:
        """Read up to byte_count bytes and pad underruns with silence."""
        if byte_count <= 0 or byte_count % self._alignment:
            raise ValueError("byte_count must be a positive whole number of samples")

        async with self._lock:
            take = min(byte_count, len(self._data))
            chunk = bytes(self._data[:take])
            del self._data[:take]

        return chunk + bytes(byte_count - take)

    async def clear(self) -> int:
        """Clear queued audio and return the number of discarded bytes."""
        async with self._lock:
            removed = len(self._data)
            self._data.clear()
            return removed

    async def size(self) -> int:
        """Return the current number of buffered bytes."""
        async with self._lock:
            return len(self._data)


class IVSInputStream(InputStream):
    """Convert an incoming IVS aiortc audio track into Strands PCM deltas."""

    def __init__(
        self,
        pcm_format: PCMFormat = DEFAULT_INPUT_FORMAT,
        *,
        track: IVSAudioTrack | None = None,
        own_track: bool = False,
    ) -> None:
        if pcm_format.channels != 1:
            raise ValueError("Nova Sonic input must be mono")

        self.pcm_format = pcm_format
        self._track = track
        self._own_track = own_track
        self._track_ready = asyncio.Event()
        if track is not None:
            self._track_ready.set()
        self._pending_pcm: deque[bytes] = deque()
        self._resampler: AudioResampler | None = None
        self._started = False
        self._stopped = False
        self._track_stopped = False

    @property
    def started(self) -> bool:
        """Whether Strands has started this stream."""
        return self._started

    @property
    def stopped(self) -> bool:
        """Whether cleanup has run."""
        return self._stopped

    def attach_track(self, track: IVSAudioTrack) -> None:
        """Attach the participant audio track delivered by aiortc."""
        if track.kind != "audio":
            raise ValueError("IVSInputStream only accepts audio tracks")
        if self._track is not None and self._track is not track:
            raise RuntimeError("an IVS audio track is already attached")

        self._track = track
        self._track_ready.set()

    async def start(self, agent: BidiAgent) -> None:
        """Validate Nova input settings and initialise the IVS resampler."""
        if self._started:
            raise RuntimeError("IVS input stream already started")
        if not isinstance(agent.model, AudioCapable):
            raise TypeError("IVSInputStream requires an audio-capable model")

        _validate_model_stream(agent.model.get_audio_config()["input"], self.pcm_format, "input")
        self._resampler = AudioResampler(
            format="s16",
            layout="mono",
            rate=self.pcm_format.sample_rate,
        )
        self._pending_pcm.clear()
        self._started = True
        self._stopped = False

    async def __call__(self) -> AudioDelta:
        """Read and resample one IVS audio frame."""
        if not self._started or self._resampler is None:
            raise RuntimeError("IVS input stream is not started")

        while not self._pending_pcm:
            if self._stopped:
                raise asyncio.CancelledError

            await self._track_ready.wait()
            if self._stopped:
                raise asyncio.CancelledError
            if self._track is None:
                continue

            frame = await self._track.recv()
            converted_frames = self._resampler.resample(frame) or []
            self._pending_pcm.extend(
                pcm for converted in converted_frames if (pcm := pcm_bytes_from_frame(converted))
            )

        return AudioDelta(format="pcm", source={"bytes": self._pending_pcm.popleft()})

    async def stop(self) -> None:
        """Release resampler state and optionally stop the attached IVS track."""
        if self._stopped:
            return

        self._stopped = True
        self._started = False
        self._track_ready.set()
        self._pending_pcm.clear()
        self._resampler = None

        if self._own_track and self._track is not None and not self._track_stopped:
            result = self._track.stop()
            if inspect.isawaitable(result):
                await result
            self._track_stopped = True


class IVSOutputStream(OutputStream):
    """Buffer Strands Nova Sonic PCM output for an IVS aiortc audio track."""

    def __init__(
        self,
        pcm_format: PCMFormat = DEFAULT_OUTPUT_FORMAT,
        *,
        max_buffer_ms: int = 2000,
        transcript_handler: Callable[[Role, str], Awaitable[None] | None] | None = None,
    ) -> None:
        if pcm_format.channels != 1:
            raise ValueError("Nova Sonic output must be mono")
        if max_buffer_ms <= 0:
            raise ValueError("max_buffer_ms must be positive")

        max_bytes = pcm_format.sample_rate * pcm_format.channels * 2 * max_buffer_ms // 1000
        self.pcm_format = pcm_format
        self._buffer = _PCMBuffer(max_bytes=max_bytes, alignment=pcm_format.channels * 2)
        self._transcript_handler = transcript_handler
        self._started = False
        self._stopped = False
        self.barge_in_count = 0
        self.barge_in_cleared_bytes = 0
        self.overflow_dropped_bytes = 0

    @property
    def started(self) -> bool:
        """Whether Strands has started this stream."""
        return self._started

    @property
    def stopped(self) -> bool:
        """Whether cleanup has run."""
        return self._stopped

    async def start(self, agent: BidiAgent) -> None:
        """Validate Nova output settings and open the playback buffer."""
        if self._started:
            raise RuntimeError("IVS output stream already started")
        if not isinstance(agent.model, AudioCapable):
            raise TypeError("IVSOutputStream requires an audio-capable model")

        _validate_model_stream(agent.model.get_audio_config()["output"], self.pcm_format, "output")
        await self._buffer.start()
        self._started = True
        self._stopped = False

    async def __call__(self, event: BidiOutputEvent) -> None:
        """Handle PCM, barge-in, connection-stop, and completed transcript events."""
        if not self._started:
            raise RuntimeError("IVS output stream is not started")

        if isinstance(event, BidiAudioDeltaEvent):
            _validate_model_stream(
                AudioStreamConfig(
                    format=event.format,
                    sample_rate=event.sample_rate,
                    channels=event.channels,
                ),
                self.pcm_format,
                "event",
            )
            try:
                pcm = base64.b64decode(event.audio, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("audio event contains invalid base64") from error
            self.overflow_dropped_bytes += await self._buffer.put(pcm)
            return

        if isinstance(event, BidiBargeInEvent):
            self.barge_in_count += 1
            self.barge_in_cleared_bytes += await self._buffer.clear()
            return

        if isinstance(event, BidiConnectionStopEvent):
            await self._buffer.clear()
            return

        if self._transcript_handler is not None and event.get("type") == "bidi_transcript_block":
            result = self._transcript_handler(event["role"], event["transcript"])
            if inspect.isawaitable(result):
                await result

    async def read_pcm_frame(self) -> bytes:
        """Return one real-time PCM frame, padded with silence on underrun."""
        return await self._buffer.read(self.pcm_format.bytes_per_frame)

    async def buffered_bytes(self) -> int:
        """Return queued PCM byte count."""
        return await self._buffer.size()

    async def stop(self) -> None:
        """Discard queued speech and close the playback buffer."""
        if self._stopped:
            return

        self._stopped = True
        self._started = False
        await self._buffer.stop()


class IVSOutputAudioTrack(AudioStreamTrack):
    """aiortc pull track backed by an IVSOutputStream."""

    kind = "audio"

    def __init__(self, output: IVSOutputStream) -> None:
        super().__init__()
        self._output = output
        self._pts = 0
        self._next_deadline: float | None = None

    async def recv(self) -> AudioFrame:
        """Return the next paced audio frame for IVS publishing."""
        loop = asyncio.get_running_loop()
        frame_duration_s = self._output.pcm_format.frame_duration_ms / 1000

        if self._next_deadline is None:
            self._next_deadline = loop.time()
        else:
            self._next_deadline += frame_duration_s
            delay = self._next_deadline - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -frame_duration_s:
                self._next_deadline = loop.time()

        pcm = await self._output.read_pcm_frame()
        frame = AudioFrame(
            format="s16",
            layout="mono",
            samples=self._output.pcm_format.samples_per_frame,
        )
        frame.planes[0].update(pcm)
        frame.sample_rate = self._output.pcm_format.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self._output.pcm_format.sample_rate)
        self._pts += frame.samples
        return frame


class IVSBlankVideoTrack(VideoStreamTrack):
    """Blank yuv420p video track so IVS can negotiate an H.264 publisher."""

    kind = "video"

    def __init__(self, width: int = 320, height: int = 180) -> None:
        super().__init__()
        if width <= 0 or height <= 0:
            raise ValueError("video dimensions must be positive")
        if width % 2 or height % 2:
            raise ValueError("yuv420p video dimensions must be even")
        self.width = width
        self.height = height

    async def recv(self) -> VideoFrame:
        """Return the next timed black frame."""
        pts, time_base = await self.next_timestamp()
        frame = VideoFrame(self.width, self.height, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = pts
        frame.time_base = time_base
        return frame
