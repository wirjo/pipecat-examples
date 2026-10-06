import asyncio
from types import SimpleNamespace

import numpy as np
from aiortc import MediaStreamTrack
from av import AudioFrame
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    CancelFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from amazon_ivs_pipecat.track import BufferedPCM16AudioTrack
from amazon_ivs_pipecat.transport import (
    IVSInputTransport,
    IVSOutputTransport,
    IVSTransportParams,
)


class BlockingInputTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = asyncio.Event()

    async def recv(self) -> AudioFrame:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


def setup_context() -> FrameProcessorSetup:
    return FrameProcessorSetup(
        clock=SystemClock(),
        task_manager=TaskManager(),
        pipeline_worker=SimpleNamespace(app_resources=None),
        audio_in_sample_rate=16_000,
        audio_out_sample_rate=24_000,
    )


async def test_input_cancel_stops_blocked_aiortc_recv() -> None:
    track = BlockingInputTrack()
    transport = IVSInputTransport(track, IVSTransportParams())
    await transport.setup(setup_context())
    await transport.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)

    assert transport.receive_task_active

    await transport.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)

    assert not transport.receive_task_active
    assert track.cancelled.is_set()
    await transport.cleanup()


async def test_interruption_clears_pipecat_and_aiortc_output_buffers() -> None:
    params = IVSTransportParams(
        audio_out_sample_rate=24_000,
        audio_out_10ms_chunks=2,
        output_frame_duration_ms=20,
        output_max_buffer_ms=200,
    )
    track = BufferedPCM16AudioTrack(sample_rate=24_000, pace=False)
    transport = IVSOutputTransport(track, params)
    await transport.setup(setup_context())
    await transport.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)

    sender = transport._media_senders[None]
    pcm = np.arange(sender.audio_chunk_size // 2, dtype="<i2").tobytes()
    await transport.process_frame(
        OutputAudioRawFrame(audio=pcm, sample_rate=24_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await asyncio.sleep(0.05)
    assert track.buffered_bytes == sender.audio_chunk_size

    await transport.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    assert track.buffered_bytes == 0
    assert track.buffer.interruptions == 1
    assert sender._audio_queue.empty()
    await transport.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)
    assert track.buffer.closed
    await transport.cleanup()
