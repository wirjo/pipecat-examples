#!/usr/bin/env python3
"""Run a live IVS -> Strands/Nova Sonic -> IVS audio validation."""

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
from ivs_loopback import (
    load_participant,
    observe_track,
    publish,
    subscribe_audio,
    wait_connected,
)
from ivs_strands.adapters import IVSInputStream, IVSOutputAudioTrack, IVSOutputStream
from ivs_strands.app import INPUT_FORMAT, OUTPUT_FORMAT
from ivs_strands.ivs import (
    join_as_audio_publisher,
    join_as_audio_subscriber,
    parse_stage_token,
)
from strands.bidi import BidiAgent
from strands.bidi.models import BedrockNovaSonicAudioConfig, BedrockNovaSonicModel


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


async def wait_streams_ready(
    input_stream: IVSInputStream,
    output_stream: IVSOutputStream,
    timeout: float = 15,
) -> None:
    async def poll() -> None:
        while not input_stream.started or not output_stream.started:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


async def run(args: argparse.Namespace) -> dict[str, object]:
    source_token, source_id = load_participant(args.source_token_file)
    agent_token, agent_id = load_participant(args.agent_token_file)
    sink_token, _ = load_participant(args.sink_token_file)

    prompt_track = GatedPCMAudioTrack(args.prompt_pcm)
    source_pc = await publish(source_token, prompt_track)

    transcripts: list[dict[str, object]] = []

    async def capture_transcript(role: str, transcript: str) -> None:
        transcripts.append({"role": str(role), "characters": len(transcript)})

    input_stream = IVSInputStream(INPUT_FORMAT, own_track=True)
    output_stream = IVSOutputStream(
        OUTPUT_FORMAT,
        max_buffer_ms=2_000,
        transcript_handler=capture_transcript,
    )
    output_track = IVSOutputAudioTrack(output_stream)
    audio: BedrockNovaSonicAudioConfig = {
        "input": {"sample_rate": INPUT_FORMAT.sample_rate},
        "output": {"sample_rate": OUTPUT_FORMAT.sample_rate},
    }
    agent = BidiAgent(
        model=BedrockNovaSonicModel(
            model_id=args.model_id,
            region=args.region,
            voice=args.voice,
            audio=audio,
        ),
        system_prompt=(
            "You are validating a voice pipeline. Reply to the user in one short "
            "sentence, then wait."
        ),
    )

    stopped = asyncio.Event()
    agent_publish_pc = await join_as_audio_publisher(
        agent_token,
        output_track,
        on_disconnect=stopped.set,
    )
    await wait_connected(agent_publish_pc)
    agent_subscribe_pc = await join_as_audio_subscriber(
        agent_token,
        parse_stage_token(agent_token),
        source_id,
        input_stream,
        on_disconnect=stopped.set,
    )
    await wait_connected(agent_subscribe_pc)
    sink_pc, sink_track = await subscribe_audio(sink_token, agent_id)

    agent_task = asyncio.create_task(agent.run(inputs=[input_stream], outputs=[output_stream]))
    try:
        await wait_streams_ready(input_stream, output_stream)
        prompt_track.play.set()
        observation = await observe_track(sink_track, sink_pc, args.frames)
        return {
            "observation": asdict(observation),
            "transcripts": transcripts,
            "barge_in_count": output_stream.barge_in_count,
            "agent_stopped_early": agent_task.done(),
        }
    finally:
        if not agent_task.done():
            agent_task.cancel()
        with suppress(asyncio.CancelledError):
            await agent_task
        output_track.stop()
        for pc in (agent_subscribe_pc, agent_publish_pc, source_pc):
            with suppress(Exception):
                await pc.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-token-file", type=Path, required=True)
    parser.add_argument("--agent-token-file", type=Path, required=True)
    parser.add_argument("--sink-token-file", type=Path, required=True)
    parser.add_argument("--prompt-pcm", type=Path, required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--model-id", default="amazon.nova-2-sonic-v1:0")
    parser.add_argument("--voice", default="tiffany")
    parser.add_argument("--frames", type=int, default=750)
    args = parser.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2))
    nonzero = result["observation"]["nonzero_frames"]
    if not isinstance(nonzero, int) or nonzero < 5:
        raise SystemExit("Expected Nova Sonic audio to return through IVS")


if __name__ == "__main__":
    main()
