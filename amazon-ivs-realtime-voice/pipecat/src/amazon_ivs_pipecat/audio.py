"""PCM conversion between PyAV/aiortc and Pipecat audio frames."""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from av import AudioFrame, AudioResampler
from pipecat.frames.frames import InputAudioRawFrame

PCM16_SAMPLE_BYTES = 2
NANOSECONDS_PER_SECOND = 1_000_000_000


def pcm16_frame_width(num_channels: int) -> int:
    """Return bytes per interleaved PCM16 sample frame."""
    if num_channels < 1:
        raise ValueError("num_channels must be positive")
    return PCM16_SAMPLE_BYTES * num_channels


def validate_pcm16(audio: bytes, num_channels: int) -> None:
    """Reject PCM that ends in a partial interleaved sample frame."""
    frame_width = pcm16_frame_width(num_channels)
    if len(audio) % frame_width:
        raise ValueError(
            f"PCM16 byte length {len(audio)} is not aligned to {num_channels} channel(s)"
        )


def av_frame_pts_nanoseconds(frame: AudioFrame) -> int | None:
    """Convert a PyAV frame PTS and time base to Pipecat nanoseconds."""
    if frame.pts is None or frame.time_base is None:
        return None
    return int(frame.pts * frame.time_base * NANOSECONDS_PER_SECOND)


def pcm16_bytes_to_av_frame(
    audio: bytes,
    *,
    sample_rate: int,
    num_channels: int,
    pts_samples: int,
) -> AudioFrame:
    """Build a packed signed-16 PyAV frame from interleaved little-endian PCM."""
    validate_pcm16(audio, num_channels)
    layout = "mono" if num_channels == 1 else "stereo"
    samples = np.frombuffer(audio, dtype="<i2")
    packed = samples.reshape(1, -1)
    frame = AudioFrame.from_ndarray(packed, format="s16", layout=layout)
    frame.sample_rate = sample_rate
    frame.pts = pts_samples
    frame.time_base = Fraction(1, sample_rate)
    return frame


class AiortcPcmIngressConverter:
    """Stateful PyAV resampler that emits Pipecat input audio frames."""

    def __init__(
        self,
        *,
        sample_rate: int,
        num_channels: int = 1,
        source: str | None = "ivs",
    ) -> None:
        if sample_rate < 1:
            raise ValueError("sample_rate must be positive")
        if num_channels not in (1, 2):
            raise ValueError("this sample supports mono or stereo PCM")

        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.source = source
        layout = "mono" if num_channels == 1 else "stereo"
        self._resampler = AudioResampler(format="s16", layout=layout, rate=sample_rate)

    def convert(self, frame: AudioFrame) -> list[InputAudioRawFrame]:
        """Resample one aiortc frame into zero or more Pipecat PCM frames."""
        converted: list[InputAudioRawFrame] = []
        for resampled in self._resampler.resample(frame):
            array = resampled.to_ndarray()
            pcm = np.asarray(array, dtype="<i2").tobytes(order="C")
            validate_pcm16(pcm, self.num_channels)

            output = InputAudioRawFrame(
                audio=pcm,
                sample_rate=self.sample_rate,
                num_channels=self.num_channels,
            )
            output.pts = av_frame_pts_nanoseconds(resampled)
            output.transport_source = self.source
            converted.append(output)
        return converted
