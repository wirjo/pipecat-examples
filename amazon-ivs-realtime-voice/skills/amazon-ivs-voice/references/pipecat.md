# Pipecat integration

Use this path when the target application builds a Pipecat `Pipeline`. A
`StrandsAgentsProcessor` inside that pipeline does not change the transport choice.

## Install the merged transport

Add the Amazon IVS Pipecat extra using the target project's package manager:

```bash
uv add "pipecat-ai[amazon-ivs]"
```

Keep the target project's existing AWS, STT, LLM, TTS, VAD, and runner extras.

## Replace the media boundary

Import the merged transport:

```python
from pipecat.transports.amazon_ivs import AmazonIVSParams, AmazonIVSTransport
```

Create the transport from values returned by the trusted backend:

```python
transport = AmazonIVSTransport(
    participant_token=ivs_session.agent_participant_token,
    subscribe_participant_id=ivs_session.source_participant_id,
    subscription_url=ivs_session.subscription_url,
    params=AmazonIVSParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
)
```

Place the IVS processors around the existing pipeline. Keep every existing processor in its
current relative order:

```python
pipeline = Pipeline(
    [
        transport.input(),
        *existing_voice_processors,
        transport.output(),
    ]
)
```

In a cascaded pipeline, `existing_voice_processors` normally contains STT, user context,
the text model or `StrandsAgentsProcessor`, TTS, and assistant context. Do not replace those
components while adding IVS.

## Preserve lifecycle behaviour

Keep the application's existing Pipecat worker or task runner. Register transport events to
cancel the active worker when the source participant disconnects, and surface `on_error`
without logging the token or subscription URL.

Pipecat interruption frames clear transport output that remains interruptible. Add a focused
test that queues output, triggers interruption, and confirms stale PCM does not play.

## Verify

Run the target project's normal unit tests, then add:

- A pipeline-order test with `transport.input()` first and `transport.output()` after speech
  generation.
- A configuration test that rejects input mode without a source participant and WHEP URL.
- An interruption test.
- An idempotent cleanup test.
- An opt-in live IVS audio traversal check.

Use the complete [Pipecat sample](../../../pipecat/) for runner and test patterns.

