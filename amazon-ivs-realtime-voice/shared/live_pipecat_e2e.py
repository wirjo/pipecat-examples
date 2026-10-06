#!/usr/bin/env python3
"""Run a live IVS -> Pipecat cascaded services -> IVS audio validation."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import suppress
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc import AudioStreamTrack
from amazon_ivs_pipecat import (
    CascadedServices,
    IVSTransportParams,
    create_cascaded_session,
)
from ivs_loopback import (
    load_participant,
    observe_track,
    publish,
    subscribe_audio,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregatorParams,
)
from pipecat.services.aws.llm import AWSBedrockLLMService
from pipecat.services.aws.stt import AWSTranscribeSTTService
from pipecat.services.aws.tts import AWSPollyTTSService
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.workers.base_worker import WorkerParams


class GatedPCMAudioTrack(AudioStreamTrack):
    """Publish a PCM prompt after all stage participants are connected."""

    kind = "audio"

    def __init__(self, pcm_path: Path, sample_rate: int = 16_000) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.samples_per_frame = sample_rate // 50
        self.bytes_per_frame = self.samples_per_frame * 2
        self.position = 0
        self.play = asyncio.Event()
        self._pcm = pcm_path.read_bytes()
        self._offset = 0

    async def recv(self) -> av.AudioFrame:
        if self.play.is_set() and self._offset < len(self._pcm):
            chunk = self._pcm[self._offset : self._offset + self.bytes_per_frame]
            self._offset += len(chunk)
        else:
            chunk = b""
        chunk += bytes(self.bytes_per_frame - len(chunk))
        samples = np.frombuffer(chunk, dtype="<i2")
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


async def run(args: argparse.Namespace) -> dict[str, object]:
    source_token, source_id = load_participant(args.source_token_file)
    agent_token, agent_id = load_participant(args.agent_token_file)
    sink_token, _ = load_participant(args.sink_token_file)

    prompt_track = GatedPCMAudioTrack(args.prompt_pcm)
    source_pc = await publish(source_token, prompt_track)
    agent_subscribe_pc, agent_input_track = await subscribe_audio(
        agent_token,
        source_id,
    )

    services = CascadedServices(
        stt=AWSTranscribeSTTService(region=args.region),
        llm=AWSBedrockLLMService(
            aws_region=args.region,
            settings=AWSBedrockLLMService.Settings(
                model=args.model_id,
                temperature=0,
                max_tokens=80,
                system_instruction=(
                    "You are validating a voice pipeline. Reply in one short sentence, then wait."
                ),
            ),
        ),
        tts=AWSPollyTTSService(
            region=args.region,
            settings=AWSPollyTTSService.Settings(
                voice=args.voice,
                engine="neural",
                rate="1.0",
            ),
        ),
    )
    session = create_cascaded_session(
        input_track=agent_input_track,
        services=services,
        transport_params=IVSTransportParams(
            audio_in_sample_rate=16_000,
            audio_out_sample_rate=24_000,
        ),
        user_aggregator_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )
    agent_publish_pc = await publish(agent_token, session.transport.output_track)
    sink_pc, sink_track = await subscribe_audio(sink_token, agent_id)

    pipeline_started = asyncio.Event()

    @session.worker.event_handler("on_pipeline_started")
    async def on_pipeline_started(worker, frame) -> None:
        pipeline_started.set()

    worker_task = asyncio.create_task(session.worker.run(WorkerParams(task_manager=TaskManager())))
    try:
        await asyncio.wait_for(pipeline_started.wait(), 20)
        prompt_track.play.set()
        observation = await observe_track(sink_track, sink_pc, args.frames)
        messages = session.cascaded.context.get_messages(truncate_large_values=True)
        return {
            "observation": asdict(observation),
            "context_message_count": len(messages),
            "context_roles": [str(message.get("role", "")) for message in messages],
            "worker_stopped_early": worker_task.done(),
        }
    finally:
        if not worker_task.done():
            await session.worker.queue_frame(EndFrame())
        with suppress(asyncio.CancelledError):
            await worker_task
        for pc in (agent_publish_pc, agent_subscribe_pc, source_pc):
            with suppress(Exception):
                await pc.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-token-file", type=Path, required=True)
    parser.add_argument("--agent-token-file", type=Path, required=True)
    parser.add_argument("--sink-token-file", type=Path, required=True)
    parser.add_argument("--prompt-pcm", type=Path, required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--model-id", default="amazon.nova-lite-v1:0")
    parser.add_argument("--voice", default="Joanna")
    parser.add_argument("--frames", type=int, default=900)
    args = parser.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, default=str))
    nonzero = result["observation"]["nonzero_frames"]
    if not isinstance(nonzero, int) or nonzero < 5:
        raise SystemExit("Expected cascaded Pipecat audio to return through IVS")


if __name__ == "__main__":
    main()
