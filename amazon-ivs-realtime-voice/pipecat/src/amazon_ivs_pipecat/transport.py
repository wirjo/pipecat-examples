"""Pipecat transport processors around aiortc audio tracks."""

from __future__ import annotations

import asyncio

from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from .audio import AiortcPcmIngressConverter
from .track import BufferedPCM16AudioTrack


class IVSTransportParams(TransportParams):
    """Audio-only transport settings for one IVS participant session."""

    audio_in_enabled: bool = True
    audio_in_sample_rate: int | None = 16_000
    audio_in_channels: int = 1
    audio_in_passthrough: bool = True
    audio_out_enabled: bool = True
    audio_out_sample_rate: int | None = 24_000
    audio_out_channels: int = 1
    audio_out_10ms_chunks: int = 4
    audio_out_end_silence_secs: int = 0
    audio_out_auto_silence: bool = False
    input_recv_timeout_secs: float = 5.0
    input_source: str | None = "ivs"
    output_frame_duration_ms: int = 20
    output_max_buffer_ms: int = 2_000


class IVSInputTransport(BaseInputTransport):
    """Read PyAV frames from an injected aiortc track and push Pipecat PCM frames."""

    def __init__(
        self,
        input_track: MediaStreamTrack | None,
        params: IVSTransportParams,
        **kwargs,
    ) -> None:
        super().__init__(params, **kwargs)
        self._input_track = input_track
        self._params = params
        self._converter: AiortcPcmIngressConverter | None = None
        self._receive_task: asyncio.Task | None = None
        self._started = False

    @property
    def receive_task_active(self) -> bool:
        """Return whether the aiortc receive loop is running."""
        return self._receive_task is not None and not self._receive_task.done()

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await super().setup(setup)
        self._converter = AiortcPcmIngressConverter(
            sample_rate=self.sample_rate,
            num_channels=self._params.audio_in_channels,
            source=self._params.input_source,
        )

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._started = True
        self._start_receive_task()
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame) -> None:
        self._started = False
        await self._stop_receive_task()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        self._started = False
        await self._stop_receive_task()
        await super().cancel(frame)

    async def cleanup(self) -> None:
        self._started = False
        await self._stop_receive_task()
        await super().cleanup()

    async def attach_track(self, input_track: MediaStreamTrack) -> None:
        """Replace the input track, supporting IVS subscription after setup."""
        await self._stop_receive_task()
        self._input_track = input_track
        self._start_receive_task()

    def _start_receive_task(self) -> None:
        if (
            self._started
            and self._params.audio_in_enabled
            and self._input_track is not None
            and self._receive_task is None
        ):
            self._receive_task = self.create_task(self._receive_audio(), name="ivs_audio_receive")

    async def _stop_receive_task(self) -> None:
        if self._receive_task is not None:
            await self.cancel_task(self._receive_task)
            self._receive_task = None

    async def _receive_audio(self) -> None:
        if self._converter is None or self._input_track is None:
            return

        while True:
            try:
                media_frame = await asyncio.wait_for(
                    self._input_track.recv(),
                    timeout=self._params.input_recv_timeout_secs,
                )
            except TimeoutError:
                continue
            except MediaStreamError:
                return

            if not isinstance(media_frame, AudioFrame):
                continue
            for pipecat_frame in self._converter.convert(media_frame):
                await self.push_audio_frame(pipecat_frame)


class IVSOutputTransport(BaseOutputTransport):
    """Write Pipecat output PCM into an injected aiortc output track."""

    def __init__(
        self,
        output_track: BufferedPCM16AudioTrack,
        params: IVSTransportParams,
        **kwargs,
    ) -> None:
        super().__init__(params, **kwargs)
        self._output_track = output_track

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        await self._output_track.close()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._output_track.close()

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._output_track.close()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # BaseOutputTransport first cancels or drains its current writer task.
            # Clearing after that prevents a stale in-flight write from refilling
            # the aiortc playout buffer.
            await self._output_track.interrupt()

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        return await self._output_track.write_frame(frame)


class IVSPipecatTransport(BaseTransport):
    """Audio-only Pipecat transport for one IVS aiortc participant session."""

    def __init__(
        self,
        *,
        input_track: MediaStreamTrack | None,
        params: IVSTransportParams | None = None,
        output_track: BufferedPCM16AudioTrack | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.params = params or IVSTransportParams()
        output_sample_rate = self.params.audio_out_sample_rate or 24_000
        self.output_track = output_track or BufferedPCM16AudioTrack(
            sample_rate=output_sample_rate,
            num_channels=self.params.audio_out_channels,
            frame_duration_ms=self.params.output_frame_duration_ms,
            max_buffer_ms=self.params.output_max_buffer_ms,
        )
        self._input = IVSInputTransport(
            input_track,
            self.params,
            name=self._input_name,
        )
        self._output = IVSOutputTransport(
            self.output_track,
            self.params,
            name=self._output_name,
        )

    def input(self) -> IVSInputTransport:
        return self._input

    def output(self) -> IVSOutputTransport:
        return self._output

    async def attach_input_track(self, input_track: MediaStreamTrack) -> None:
        """Attach or replace the IVS subscriber audio track."""
        await self._input.attach_track(input_track)
