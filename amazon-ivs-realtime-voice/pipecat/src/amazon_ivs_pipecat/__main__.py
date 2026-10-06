"""Run a deterministic, provider-free adapter and composition self-check."""

from __future__ import annotations

import asyncio
import json
from fractions import Fraction

import numpy as np
from av import AudioFrame
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from .audio import AiortcPcmIngressConverter
from .offline import OfflineLLMService, OfflineSTTService, OfflineTTSService
from .pipeline import CascadedServices, create_cascaded_session
from .track import BufferedPCM16AudioTrack


async def main() -> None:
    source_pcm = np.arange(320, dtype="<i2")
    av_input = AudioFrame.from_ndarray(source_pcm.reshape(1, -1), format="s16", layout="mono")
    av_input.sample_rate = 16_000
    av_input.pts = 160
    av_input.time_base = Fraction(1, 16_000)

    ingress = AiortcPcmIngressConverter(sample_rate=16_000, source="offline-ivs")
    pipecat_input = ingress.convert(av_input)[0]

    output_track = BufferedPCM16AudioTrack(sample_rate=16_000, pace=False)
    await output_track.write_frame(
        OutputAudioRawFrame(
            audio=pipecat_input.audio,
            sample_rate=16_000,
            num_channels=1,
        )
    )
    av_output = await output_track.recv()
    await output_track.close()

    session = create_cascaded_session(
        input_track=None,
        services=CascadedServices(
            stt=OfflineSTTService(),
            llm=OfflineLLMService(),
            tts=OfflineTTSService(),
        ),
        user_aggregator_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies()
        ),
    )
    processors = [
        processor.__class__.__name__ for processor in session.cascaded.pipeline.processors[1:-1]
    ]
    print(
        json.dumps(
            {
                "ingress_bytes": len(pipecat_input.audio),
                "ingress_pts_ns": pipecat_input.pts,
                "egress_samples": av_output.samples,
                "pipeline": processors,
                "live_provider_validation": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
