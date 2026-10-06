"""Deterministic offline tests for IVS Strands audio adapters."""

from __future__ import annotations

import base64
import struct
import unittest
from types import SimpleNamespace
from typing import cast

from av import AudioFrame
from ivs_strands.adapters import (
    IVSBlankVideoTrack,
    IVSInputStream,
    IVSOutputStream,
    PCMFormat,
    pcm_bytes_from_frame,
)
from strands.bidi import BidiAgent
from strands.bidi.types import BidiAudioDeltaEvent, BidiBargeInEvent


class FakeAudioModel:
    """Structural AudioCapable used without AWS."""

    def __init__(self, input_format: PCMFormat, output_format: PCMFormat) -> None:
        self._audio = {
            "input": input_format.as_strands_config(),
            "output": output_format.as_strands_config(),
        }

    def get_audio_config(self):
        return self._audio


class FakeTrack:
    kind = "audio"

    def __init__(self, *frames: AudioFrame) -> None:
        self.frames = list(frames)
        self.stop_calls = 0

    async def recv(self) -> AudioFrame:
        if not self.frames:
            raise RuntimeError("track ended")
        return self.frames.pop(0)

    def stop(self) -> None:
        self.stop_calls += 1


def fake_agent(
    input_format: PCMFormat | None = None,
    output_format: PCMFormat | None = None,
):
    input_format = input_format or PCMFormat(sample_rate=16000)
    output_format = output_format or PCMFormat(sample_rate=24000)
    return cast(
        BidiAgent,
        SimpleNamespace(model=FakeAudioModel(input_format, output_format)),
    )


class PCMConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_video_track_returns_black_yuv420p_frame(self) -> None:
        track = IVSBlankVideoTrack(width=320, height=180)
        frame = await track.recv()

        self.assertEqual(frame.format.name, "yuv420p")
        self.assertEqual((frame.width, frame.height), (320, 180))
        self.assertIsNotNone(frame.pts)
        self.assertTrue(all(not any(bytes(plane)) for plane in frame.planes))
        track.stop()

    async def test_input_frame_becomes_exact_strands_pcm_delta(self) -> None:
        pcm_format = PCMFormat(sample_rate=16000)
        samples = tuple(range(320))
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        frame = AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.sample_rate = pcm_format.sample_rate
        frame.planes[0].update(pcm)

        self.assertEqual(pcm_bytes_from_frame(frame), pcm)

        track = FakeTrack(frame)
        stream = IVSInputStream(pcm_format, track=track)
        await stream.start(fake_agent())
        event = await stream()

        self.assertEqual(event.format, "pcm")
        self.assertEqual(event.source.get("bytes"), pcm)
        await stream.stop()

    async def test_ivs_stereo_48khz_frame_is_resampled_to_mono_16khz(self) -> None:
        ivs_samples = 960
        interleaved = (1000, 1000) * ivs_samples
        pcm = struct.pack(f"<{len(interleaved)}h", *interleaved)
        frame = AudioFrame(format="s16", layout="stereo", samples=ivs_samples)
        frame.sample_rate = 48000
        frame.planes[0].update(pcm)

        stream = IVSInputStream(track=FakeTrack(frame))
        await stream.start(fake_agent())
        event = await stream()
        output = event.source.get("bytes")
        self.assertIsNotNone(output)
        assert output is not None
        output_samples = struct.unpack(f"<{len(output) // 2}h", output)

        self.assertEqual(len(output), 608)
        self.assertEqual(set(output_samples), {1000})
        await stream.stop()

    async def test_output_buffers_and_pulls_twenty_millisecond_frames(self) -> None:
        output_format = PCMFormat(sample_rate=24000)
        first = b"\x01\x02" * output_format.samples_per_frame
        second = b"\x03\x04" * output_format.samples_per_frame
        stream = IVSOutputStream(output_format)
        await stream.start(fake_agent())

        await stream(
            BidiAudioDeltaEvent(
                audio=base64.b64encode(first + second).decode("ascii"),
                format="pcm",
                sample_rate=output_format.sample_rate,
                channels=1,
                content_id="audio-1",
            )
        )

        self.assertEqual(await stream.buffered_bytes(), len(first + second))
        self.assertEqual(await stream.read_pcm_frame(), first)
        self.assertEqual(await stream.read_pcm_frame(), second)
        self.assertEqual(await stream.read_pcm_frame(), bytes(len(first)))
        await stream.stop()

    async def test_barge_in_discards_queued_audio(self) -> None:
        output_format = PCMFormat(sample_rate=24000)
        pcm = b"\x11\x22" * output_format.samples_per_frame
        stream = IVSOutputStream(output_format)
        await stream.start(fake_agent())

        await stream(
            BidiAudioDeltaEvent(
                audio=base64.b64encode(pcm).decode("ascii"),
                format="pcm",
                sample_rate=output_format.sample_rate,
                channels=1,
                content_id="audio-1",
            )
        )
        await stream(BidiBargeInEvent())

        self.assertEqual(await stream.buffered_bytes(), 0)
        self.assertEqual(stream.barge_in_count, 1)
        self.assertEqual(stream.barge_in_cleared_bytes, len(pcm))
        self.assertEqual(await stream.read_pcm_frame(), bytes(len(pcm)))
        await stream.stop()

    async def test_stop_is_idempotent_and_releases_owned_track_and_buffer(self) -> None:
        input_format = PCMFormat(sample_rate=16000)
        output_format = PCMFormat(sample_rate=24000)
        track = FakeTrack()
        input_stream = IVSInputStream(input_format, track=track, own_track=True)
        output_stream = IVSOutputStream(output_format)
        agent = fake_agent(input_format, output_format)

        await input_stream.start(agent)
        await output_stream.start(agent)
        await output_stream(
            BidiAudioDeltaEvent(
                audio=base64.b64encode(b"\x01\x02" * 480).decode("ascii"),
                format="pcm",
                sample_rate=output_format.sample_rate,
                channels=1,
                content_id="audio-1",
            )
        )

        await input_stream.stop()
        await output_stream.stop()
        await input_stream.stop()
        await output_stream.stop()

        self.assertEqual(track.stop_calls, 1)
        self.assertTrue(input_stream.stopped)
        self.assertTrue(output_stream.stopped)
        self.assertEqual(await output_stream.buffered_bytes(), 0)
