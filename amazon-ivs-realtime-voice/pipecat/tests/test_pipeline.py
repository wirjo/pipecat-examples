from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from amazon_ivs_pipecat.offline import (
    OfflineLLMService,
    OfflineSTTService,
    OfflineTTSService,
)
from amazon_ivs_pipecat.pipeline import CascadedServices, create_cascaded_session
from amazon_ivs_pipecat.transport import IVSInputTransport, IVSOutputTransport


def test_pipeline_composes_current_cascaded_order() -> None:
    services = CascadedServices(
        stt=OfflineSTTService(),
        llm=OfflineLLMService(),
        tts=OfflineTTSService(),
    )
    session = create_cascaded_session(
        input_track=None,
        services=services,
        user_aggregator_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies()
        ),
    )

    processors = session.cascaded.pipeline.processors[1:-1]
    assert isinstance(processors[0], IVSInputTransport)
    assert processors[1] is services.stt
    assert isinstance(processors[2], LLMUserAggregator)
    assert processors[3] is services.llm
    assert processors[4] is services.tts
    assert isinstance(processors[5], IVSOutputTransport)
    assert isinstance(processors[6], LLMAssistantAggregator)
    assert session.cascaded.pipeline in session.worker._pipeline.processors
