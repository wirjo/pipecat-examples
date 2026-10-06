# Pipecat sample

> [!WARNING]
> This path is validated as a sample, not as a production solution. It has not been
> validated for latency, scale, resilience, security, or operational support.

This package bridges audio between an Amazon IVS Real-Time stage connection
built with aiortc and a Pipecat cascaded pipeline:

```text
aiortc input track
  -> Pipecat input audio frame
  -> Amazon Transcribe
  -> conversation context
  -> Amazon Nova Lite
  -> Amazon Polly
  -> Pipecat output transport
  -> buffered aiortc output track
```

The shared live harness owns WHIP and WHEP signalling, participant tokens,
H.264 blank video, and the IVS SDP workaround. This package owns audio frame
conversion, bounded playout, interruption cleanup, and Pipecat pipeline
composition.

The package targets `pipecat-ai[aws,silero]==1.12.0` with
`aiortc>=1.14.0,<2`. This directory ships a committed `uv.lock`.

## Run the local checks

Use Python 3.12 or 3.13. Run these commands from the repository root.
The commands below use the validated Python 3.13 environment.

1. Sync the environment.

   ```console
   uv --directory amazon-ivs-realtime-voice/pipecat sync --python 3.13
   ```

2. Run the deterministic, provider-free self-check.

   ```console
   uv --directory amazon-ivs-realtime-voice/pipecat run python -m amazon_ivs_pipecat
   ```

3. Run the twelve shared harness tests.

   ```console
   uv --directory amazon-ivs-realtime-voice/pipecat run python -m pytest ../shared/test_audio_track.py
   ```

4. Run the eight Pipecat tests.

   ```console
   uv --directory amazon-ivs-realtime-voice/pipecat run python -m pytest tests
   ```

The tests cover PCM conversion, timestamps, bounded buffering, silence
padding, pipeline composition, input cancellation, HTTPS IVS host and port
validation, redirect allow-listing, and interruption cleanup.

## Parent integration

```python
import asyncio

from amazon_ivs_pipecat import (
    CascadedServices,
    IVSTransportParams,
    create_cascaded_session,
)
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.workers.base_worker import WorkerParams

session = create_cascaded_session(
    input_track=subscribed_ivs_audio_track,
    services=CascadedServices(
        stt=stt_service,
        llm=llm_service,
        tts=tts_service,
    ),
    transport_params=IVSTransportParams(),
    user_aggregator_params=user_aggregator_params,
)

publisher_peer_connection.addTransceiver(
    session.transport.output_track,
    direction="sendrecv",
)
worker_task = asyncio.create_task(
    session.worker.run(WorkerParams(task_manager=TaskManager()))
)
```

`stt_service`, `llm_service`, and `tts_service` are injected Pipecat service
instances. The parent process must configure their AWS credentials and
permissions. Add the blank H.264 transceiver before creating the publisher
offer. Queue an `EndFrame` on `session.worker`, await `worker_task`, and close
the peer connections during shutdown. The live runner shows the complete lifecycle in
[`../shared/live_pipecat_e2e.py`](../shared/live_pipecat_e2e.py). This package
does not read or store provider keys.

## Interruption behaviour

Pipecat clears queued interruptible output in `BaseOutputTransport`. This
adapter also clears PCM already handed to the aiortc output track after the base
transport cancels its writer task. That prevents stale speech from playing
after barge-in.

The output track bounds queued PCM by time. On overflow, it drops the oldest
sample-aligned audio to preserve real-time behaviour.

## Validated scope

The shared suite passed twelve tests. The Pipecat suite passed eight tests.

The live path returned 86 non-silent frames from 900 observed frames through:

```text
PCM prompt -> IVS -> Transcribe -> Nova Lite -> Polly -> IVS
```

The observer classified a frame as non-silent when its RMS amplitude exceeded
`100`. The 86/900 result proves only that non-silent audio returned through the
path. It does not measure audio continuity, transcription accuracy, response
quality, end-to-end latency, concurrency, load behaviour, or production
reliability.

The provider-neutral validation contract has not run against this package
because no Pipecat contract driver is included.
