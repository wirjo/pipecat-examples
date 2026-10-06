"""Offline integration test for BidiAgent-managed adapter cleanup."""

from __future__ import annotations

import asyncio
import struct
import unittest

from av import AudioFrame
from ivs_strands.adapters import IVSInputStream, IVSOutputStream, PCMFormat
from strands.bidi import BidiAgent
from strands.bidi.models import BidiModel
from strands.bidi.types import BidiConnectionStartEvent
from test_adapters import FakeTrack


class OfflineAudioModel(BidiModel):
    """No-network BidiModel that keeps receive open until input fails."""

    def __init__(self) -> None:
        self.config = {"model_id": "offline-audio-model"}
        self.audio = {
            "input": PCMFormat(16000).as_strands_config(),
            "output": PCMFormat(24000).as_strands_config(),
        }
        self.started = False
        self.stop_calls = 0
        self.sent = []
        self._receive_wait = asyncio.Event()

    def get_config(self):
        return self.config

    def update_config(self, **model_config) -> None:
        self.config.update(model_config)

    def get_audio_config(self):
        return self.audio

    async def start(self, system_prompt=None, tools=None, messages=None, **kwargs) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        self.stop_calls += 1
        self._receive_wait.set()

    async def send(self, content) -> None:
        self.sent.append(content)

    async def receive(self):
        yield BidiConnectionStartEvent("offline", self.model_id)
        await self._receive_wait.wait()


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_cleans_up_streams_and_model_after_input_failure(self) -> None:
        samples = tuple(range(320))
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        frame = AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.sample_rate = 16000
        frame.planes[0].update(pcm)

        track = FakeTrack(frame)
        input_stream = IVSInputStream(track=track, own_track=True)
        output_stream = IVSOutputStream()
        model = OfflineAudioModel()
        agent = BidiAgent(model=model)

        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(
                agent.run(inputs=[input_stream], outputs=[output_stream]),
                timeout=2,
            )

        self.assertEqual(len(model.sent), 1)
        self.assertEqual(model.stop_calls, 1)
        self.assertFalse(model.started)
        self.assertEqual(track.stop_calls, 1)
        self.assertTrue(input_stream.stopped)
        self.assertTrue(output_stream.stopped)
        self.assertEqual(await output_stream.buffered_bytes(), 0)
