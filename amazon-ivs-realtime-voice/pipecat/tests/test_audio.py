from fractions import Fraction

import numpy as np
import pytest
from av import AudioFrame
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame

from amazon_ivs_pipecat.audio import AiortcPcmIngressConverter, pcm16_bytes_to_av_frame
from amazon_ivs_pipecat.track import BufferedPCM16AudioTrack


def test_aiortc_ingress_conversion_preserves_pcm_and_converts_pts() -> None:
    samples = np.arange(320, dtype="<i2")
    source = AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
    source.sample_rate = 16_000
    source.pts = 160
    source.time_base = Fraction(1, 16_000)

    converted = AiortcPcmIngressConverter(
        sample_rate=16_000,
        num_channels=1,
        source="participant-123",
    ).convert(source)

    assert len(converted) == 1
    frame = converted[0]
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == samples.tobytes()
    assert frame.sample_rate == 16_000
    assert frame.num_channels == 1
    assert frame.num_frames == 320
    assert frame.pts == 10_000_000
    assert frame.transport_source == "participant-123"


def test_pcm_bytes_to_av_frame_rejects_partial_sample() -> None:
    with pytest.raises(ValueError, match="not aligned"):
        pcm16_bytes_to_av_frame(
            b"\x01",
            sample_rate=24_000,
            num_channels=1,
            pts_samples=0,
        )


async def test_output_track_converts_pipecat_pcm_to_paced_av_shape() -> None:
    samples = np.arange(480, dtype="<i2")
    track = BufferedPCM16AudioTrack(sample_rate=24_000, pace=False)
    try:
        accepted = await track.write_frame(
            OutputAudioRawFrame(
                audio=samples.tobytes(),
                sample_rate=24_000,
                num_channels=1,
            )
        )
        output = await track.recv()
    finally:
        await track.close()

    assert accepted is True
    assert output.sample_rate == 24_000
    assert output.samples == 480
    assert output.pts == 0
    assert output.time_base == Fraction(1, 24_000)
    assert output.to_ndarray().tobytes() == samples.tobytes()
