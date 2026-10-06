"""Composition helpers for the current Pipecat cascaded pipeline API."""

from __future__ import annotations

from dataclasses import dataclass

from aiortc import MediaStreamTrack
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import (
    PipelineParams,
    PipelineWorker,
    ProcessorUnusablePolicy,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from .transport import IVSPipecatTransport, IVSTransportParams


@dataclass(frozen=True)
class CascadedServices:
    """Provider services supplied by the parent integration."""

    stt: STTService
    llm: LLMService
    tts: TTSService


@dataclass(frozen=True)
class CascadedPipeline:
    """Pipeline plus the context objects a host may need to inspect or seed."""

    pipeline: Pipeline
    context: LLMContext
    user_aggregator: LLMUserAggregator
    assistant_aggregator: LLMAssistantAggregator


@dataclass(frozen=True)
class CascadedSession:
    """Transport and worker ready for a parent-owned WorkerRunner."""

    transport: IVSPipecatTransport
    cascaded: CascadedPipeline
    worker: PipelineWorker


def build_cascaded_pipeline(
    *,
    transport: IVSPipecatTransport,
    services: CascadedServices,
    context: LLMContext | None = None,
    user_aggregator_params: LLMUserAggregatorParams | None = None,
    assistant_aggregator_params: LLMAssistantAggregatorParams | None = None,
) -> CascadedPipeline:
    """Compose transport -> STT -> context -> LLM -> TTS -> transport."""
    llm_context = context or LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        llm_context,
        user_params=user_aggregator_params,
        assistant_params=assistant_aggregator_params,
        realtime_service_mode=False,
    )
    pipeline = Pipeline(
        [
            transport.input(),
            services.stt,
            user_aggregator,
            services.llm,
            services.tts,
            transport.output(),
            assistant_aggregator,
        ]
    )
    return CascadedPipeline(
        pipeline=pipeline,
        context=llm_context,
        user_aggregator=user_aggregator,
        assistant_aggregator=assistant_aggregator,
    )


def create_cascaded_session(
    *,
    input_track: MediaStreamTrack | None,
    services: CascadedServices,
    transport_params: IVSTransportParams | None = None,
    context: LLMContext | None = None,
    user_aggregator_params: LLMUserAggregatorParams | None = None,
    assistant_aggregator_params: LLMAssistantAggregatorParams | None = None,
    pipeline_params: PipelineParams | None = None,
    processor_unusable_policy: ProcessorUnusablePolicy = ProcessorUnusablePolicy.END,
) -> CascadedSession:
    """Create the transport and current Pipecat PipelineWorker without running it."""
    params = transport_params or IVSTransportParams()
    transport = IVSPipecatTransport(input_track=input_track, params=params)
    cascaded = build_cascaded_pipeline(
        transport=transport,
        services=services,
        context=context,
        user_aggregator_params=user_aggregator_params,
        assistant_aggregator_params=assistant_aggregator_params,
    )
    worker = PipelineWorker(
        cascaded.pipeline,
        params=pipeline_params
        or PipelineParams(
            audio_in_sample_rate=params.audio_in_sample_rate or 16_000,
            audio_out_sample_rate=params.audio_out_sample_rate or 24_000,
        ),
        processor_unusable_policy=processor_unusable_policy,
    )
    return CascadedSession(transport=transport, cascaded=cascaded, worker=worker)
