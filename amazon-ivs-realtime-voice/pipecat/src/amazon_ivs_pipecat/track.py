"""aiortc output track backed by Pipecat PCM frames."""

from __future__ import annotations

import asyncio
import time

from aiortc import AudioStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from pipecat.frames.frames import OutputAudioRawFrame

from .audio import pcm16_bytes_to_av_frame
from .buffer import PCM16PlayoutBuffer


class BufferedPCM16AudioTrack(AudioStreamTrack):
    """Paced aiortc track that consumes bounded, interruptible PCM16 audio."""

    kind = "audio"

    def __init__(
        self,
        *,
        sample_rate: int = 24_000,
        num_channels: int = 1,
        frame_duration_ms: int = 20,
        max_buffer_ms: int = 2_000,
        pace: bool = True,
    ) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.buffer = PCM16PlayoutBuffer(
            sample_rate=sample_rate,
            num_channels=num_channels,
            frame_duration_ms=frame_duration_ms,
            max_buffer_ms=max_buffer_ms,
        )
        self._pace = pace
        self._next_deadline: float | None = None
        self._pts_samples = 0

    @property
    def buffered_bytes(self) -> int:
        """Return PCM waiting for aiortc to request it."""
        return self.buffer.buffered_bytes

    async def write_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Accept a Pipecat output frame after BaseOutputTransport normalization."""
        if frame.sample_rate != self.sample_rate:
            raise ValueError(f"expected {self.sample_rate} Hz PCM, received {frame.sample_rate} Hz")
        if frame.num_channels != self.num_channels:
            raise ValueError(
                f"expected {self.num_channels} channel(s), received {frame.num_channels}"
            )
        return await self.buffer.append(frame.audio)

    async def interrupt(self) -> None:
        """Discard speech already queued for WebRTC playout."""
        await self.buffer.interrupt()

    async def close(self) -> None:
        """Stop the track and discard pending audio."""
        await self.buffer.close()
        self.stop()

    async def recv(self) -> AudioFrame:
        """Return one fixed-duration PyAV frame, using silence on underrun."""
        if self.readyState != "live" or self.buffer.closed:
            raise MediaStreamError

        if self._pace:
            now = time.monotonic()
            if self._next_deadline is None:
                self._next_deadline = now
            delay = self._next_deadline - now
            if delay > 0:
                await asyncio.sleep(delay)

        pcm = await self.buffer.pop_chunk()
        frame = pcm16_bytes_to_av_frame(
            pcm,
            sample_rate=self.sample_rate,
            num_channels=self.num_channels,
            pts_samples=self._pts_samples,
        )
        self._pts_samples += self.buffer.samples_per_frame
        if self._pace:
            assert self._next_deadline is not None
            self._next_deadline += self.buffer.frame_duration_ms / 1_000
        return frame
