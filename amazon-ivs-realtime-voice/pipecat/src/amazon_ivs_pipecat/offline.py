"""Deterministic provider doubles used only by tests and the offline self-check."""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator

import numpy as np
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings, STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService


def _offline_llm_settings() -> LLMSettings:
    return LLMSettings(
        model="offline-llm",
        system_instruction=None,
        temperature=None,
        max_tokens=None,
        top_p=None,
        top_k=None,
        frequency_penalty=None,
        presence_penalty=None,
        seed=None,
        filter_incomplete_user_turns=False,
        user_turn_completion_config=None,
    )


class OfflineSTTService(STTService):
    """Emit a fixed transcript for any non-empty PCM frame."""

    def __init__(self, transcript: str = "hello from ivs") -> None:
        super().__init__(
            audio_passthrough=False,
            sample_rate=16_000,
            settings=STTSettings(model="offline-stt", language=None),
        )
        self.transcript = transcript

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if audio:
            yield TranscriptionFrame(text=self.transcript, user_id="offline", timestamp="")


class OfflineLLMService(LLMService):
    """Answer each context frame with a fixed short response."""

    def __init__(self, response: str = "offline response") -> None:
        super().__init__(settings=_offline_llm_settings())
        self.response = response

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(self.response))
        await self.push_frame(LLMFullResponseEndFrame())


class OfflineTTSService(TTSService):
    """Generate deterministic mono PCM tones without a network provider."""

    def __init__(self, *, sample_rate: int = 24_000, duration_ms: int = 80) -> None:
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            push_text_frames=False,
            sample_rate=sample_rate,
            settings=TTSSettings(model="offline-tts", voice="tone", language=None),
        )
        self._offline_sample_rate = sample_rate
        self.duration_ms = duration_ms

    async def run_tts(
        self,
        text: str,
        context_id: str,
    ) -> AsyncGenerator[Frame | None, None]:
        sample_count = self._offline_sample_rate * self.duration_ms // 1_000
        phase = np.arange(sample_count, dtype=np.float64)
        amplitude = min(12_000, 1_000 + len(text) * 100)
        tone = (np.sin(2 * math.pi * 440 * phase / self._offline_sample_rate) * amplitude).astype(
            "<i2"
        )
        yield TTSAudioRawFrame(
            audio=tone.tobytes(),
            sample_rate=self._offline_sample_rate,
            num_channels=1,
            context_id=context_id,
        )
