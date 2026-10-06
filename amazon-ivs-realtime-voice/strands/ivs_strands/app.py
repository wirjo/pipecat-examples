"""Executable IVS Real-Time to Strands BidiAgent sample."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from contextlib import suppress

from strands.bidi import BidiAgent
from strands.bidi.models import BedrockNovaSonicAudioConfig, BedrockNovaSonicModel

from .adapters import IVSInputStream, IVSOutputAudioTrack, IVSOutputStream, PCMFormat
from .ivs import (
    join_as_audio_publisher,
    join_as_audio_subscriber,
    parse_stage_token,
    require_stage_capabilities,
)

logger = logging.getLogger(__name__)

INPUT_FORMAT = PCMFormat(sample_rate=16000, channels=1, frame_duration_ms=20)
OUTPUT_FORMAT = PCMFormat(sample_rate=24000, channels=1, frame_duration_ms=20)


def _model(args: argparse.Namespace) -> BedrockNovaSonicModel:
    audio: BedrockNovaSonicAudioConfig = {
        "input": {"sample_rate": 16000},
        "output": {"sample_rate": 24000},
    }
    return BedrockNovaSonicModel(
        model_id=args.model_id,
        region=args.region,
        voice=args.voice,
        audio=audio,
    )


async def _check_bedrock(args: argparse.Namespace) -> None:
    """Open and cleanly close a Nova Sonic stream without sending user audio."""
    model = _model(args)
    started = False
    try:
        await asyncio.wait_for(
            model.start(
                system_prompt="Handshake check only. Do not generate a response.",
                tools=[],
                messages=[],
            ),
            timeout=args.handshake_timeout,
        )
        started = True
        logger.info("Nova Sonic model handshake succeeded")
    finally:
        if started or getattr(model, "_connection_id", None) is not None:
            with suppress(Exception):
                await asyncio.wait_for(model.stop(), timeout=args.handshake_timeout)


async def _print_transcript(role: str, transcript: str) -> None:
    logger.info("%s transcript received (%d characters)", role, len(transcript))


async def _run_stage(args: argparse.Namespace) -> None:
    token = os.environ.get(args.token_env)
    if not token:
        raise ValueError(
            f"set the IVS participant token in the {args.token_env} environment variable"
        )
    if not args.subscribe_to:
        raise ValueError("--subscribe-to is required unless --check-bedrock is used")

    payload = parse_stage_token(token)
    require_stage_capabilities(payload)

    stop_event = asyncio.Event()
    input_stream = IVSInputStream(INPUT_FORMAT, own_track=True)
    output_stream = IVSOutputStream(
        OUTPUT_FORMAT,
        max_buffer_ms=args.max_buffer_ms,
        transcript_handler=_print_transcript,
    )
    output_track = IVSOutputAudioTrack(output_stream)
    agent = BidiAgent(
        model=_model(args),
        system_prompt=args.system_prompt,
    )

    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)
            installed_signals.append(signum)

    peers = []
    timer_task: asyncio.Task[None] | None = None
    agent_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None

    try:
        logger.info("Joining IVS stage as audio and blank-video publisher")
        peers.append(
            await join_as_audio_publisher(
                token,
                output_track,
                on_disconnect=stop_event.set,
            )
        )

        logger.info("Subscribing to IVS participant %s", args.subscribe_to)
        peers.append(
            await join_as_audio_subscriber(
                token,
                payload,
                args.subscribe_to,
                input_stream,
                on_disconnect=stop_event.set,
            )
        )

        if args.max_seconds:

            async def stop_after_timeout() -> None:
                await asyncio.sleep(args.max_seconds)
                stop_event.set()

            timer_task = asyncio.create_task(stop_after_timeout())

        logger.info("Starting Strands BidiAgent with Nova Sonic")
        agent_task = asyncio.create_task(agent.run(inputs=[input_stream], outputs=[output_stream]))
        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {agent_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done and not agent_task.done():
            agent_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_task
        else:
            await agent_task
    finally:
        for task in (stop_task, timer_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        if agent_task is not None and not agent_task.done():
            agent_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_task

        output_track.stop()
        for pc in reversed(peers):
            with suppress(Exception):
                await pc.close()

        for signum in installed_signals:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(signum)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge one IVS Real-Time participant to Strands BidiAgent/Nova Sonic."
    )
    parser.add_argument("--subscribe-to", help="Existing IVS participant ID to subscribe to")
    parser.add_argument(
        "--token-env",
        default="IVS_STAGE_TOKEN",
        help="Environment variable containing the IVS participant token",
    )
    parser.add_argument("--region", default="us-east-1", help="Bedrock region")
    parser.add_argument(
        "--model-id",
        default="amazon.nova-2-sonic-v1:0",
        help="Nova Sonic model ID",
    )
    parser.add_argument("--voice", default="tiffany", help="Nova Sonic voice ID")
    parser.add_argument(
        "--system-prompt",
        default="You are a concise voice assistant. Keep spoken replies short.",
    )
    parser.add_argument(
        "--max-buffer-ms",
        type=int,
        default=2000,
        help="Maximum queued model audio before oldest audio is dropped",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0,
        help="Stop after this many seconds; zero runs until disconnect or signal",
    )
    parser.add_argument(
        "--check-bedrock",
        action="store_true",
        help="Open and close a Nova Sonic model stream without joining IVS",
    )
    parser.add_argument(
        "--handshake-timeout",
        type=float,
        default=20,
        help="Seconds allowed for model handshake start and cleanup",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    if args.check_bedrock:
        await _check_bedrock(args)
    else:
        await _run_stage(args)


def main() -> int:
    """CLI entry point."""
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(getattr(logging, args.log_level))
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        logger.info("Stopped")
    except Exception as error:  # noqa: BLE001 - CLI boundary reports provider and network failures.
        logger.error("%s: %s", type(error).__name__, error)
        return 1
    return 0
