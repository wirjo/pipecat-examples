"""Bounded PCM16 playout buffering for an aiortc audio track."""

from __future__ import annotations

import asyncio

from .audio import pcm16_frame_width, validate_pcm16


class PCM16PlayoutBuffer:
    """A sample-aligned FIFO that bounds latency and pads short reads with silence."""

    def __init__(
        self,
        *,
        sample_rate: int,
        num_channels: int,
        frame_duration_ms: int = 20,
        max_buffer_ms: int = 2_000,
    ) -> None:
        if sample_rate < 1:
            raise ValueError("sample_rate must be positive")
        if frame_duration_ms < 1:
            raise ValueError("frame_duration_ms must be positive")
        if max_buffer_ms < frame_duration_ms:
            raise ValueError("max_buffer_ms must hold at least one output frame")

        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.frame_duration_ms = frame_duration_ms
        self.frame_width = pcm16_frame_width(num_channels)
        samples = sample_rate * frame_duration_ms
        if samples % 1_000:
            raise ValueError("frame_duration_ms must produce a whole number of samples")
        self.samples_per_frame = samples // 1_000
        self.chunk_bytes = self.samples_per_frame * self.frame_width
        max_bytes = sample_rate * max_buffer_ms * self.frame_width // 1_000
        self.max_buffer_bytes = max(self.chunk_bytes, max_bytes - (max_bytes % self.frame_width))

        self._audio = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False
        self.dropped_bytes = 0
        self.interruptions = 0

    @property
    def closed(self) -> bool:
        """Return whether the buffer rejects new audio."""
        return self._closed

    @property
    def buffered_bytes(self) -> int:
        """Return queued bytes. Safe for diagnostics in the single event loop."""
        return len(self._audio)

    async def append(self, audio: bytes) -> bool:
        """Append PCM and drop oldest aligned samples if the latency cap is exceeded."""
        validate_pcm16(audio, self.num_channels)
        if not audio:
            return not self._closed

        async with self._lock:
            if self._closed:
                return False

            self._audio.extend(audio)
            overflow = len(self._audio) - self.max_buffer_bytes
            if overflow > 0:
                aligned = overflow + (-overflow % self.frame_width)
                del self._audio[:aligned]
                self.dropped_bytes += aligned
            return True

    async def pop_chunk(self) -> bytes:
        """Return one fixed-duration chunk, padding an underrun with PCM silence."""
        async with self._lock:
            take = min(self.chunk_bytes, len(self._audio))
            chunk = bytes(self._audio[:take])
            del self._audio[:take]
        if take < self.chunk_bytes:
            chunk += bytes(self.chunk_bytes - take)
        return chunk

    async def interrupt(self) -> None:
        """Drop all queued speech after a Pipecat interruption."""
        async with self._lock:
            self._audio.clear()
            self.interruptions += 1

    async def close(self) -> None:
        """Reject future writes and discard pending audio."""
        async with self._lock:
            self._closed = True
            self._audio.clear()
