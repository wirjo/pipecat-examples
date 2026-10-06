# Strands integration

Use this path for a native Strands `BidiAgent` that consumes and produces streaming audio.
If a Pipecat pipeline contains `StrandsAgentsProcessor`, use the Pipecat integration instead.

## Install the merged adapter

Add the released Strands Amazon IVS media-adapter extra or package using the target project's
package manager. Resolve the exact module and export names from the installed version.

Do not copy the sample's `aiortc`, WHIP, WHEP, SDP, codec, or buffering implementation into
the application. If the merged adapter is missing, report that dependency instead of creating
a private replacement.

## Wrap the existing BidiAgent

Use the merged adapter's public APIs to create:

- An input stream that receives the selected IVS participant and yields model-compatible PCM.
- An output stream that receives model audio and clears queued speech on barge-in.
- A publication track that sends fixed-duration audio frames to IVS.
- A session object or connection helpers that own publisher and subscriber peer connections.

Pass the existing agent to the streams without changing its model, prompt, tools, or messages:

```python
await agent.run(
    inputs=[ivs_media.input_stream],
    outputs=[ivs_media.output_stream],
)
```

Build `ivs_media` from the opaque agent participant token, source participant ID, and WHEP
subscription URL returned by the trusted backend. Use the actual merged adapter constructor
from the installed package.

## Preserve lifecycle behaviour

Start the IVS media session before `agent.run()`. On source disconnect, model completion,
timeout, or cancellation:

1. Cancel and await the agent task.
2. Stop the output publication track.
3. Stop the input and output streams.
4. Close subscriber and publisher peer connections.

Keep cleanup idempotent. Preserve `BidiBargeInEvent` handling so an interruption clears
already-buffered speech.

## Verify

Run the target project's normal tests, then add:

- An audio-format compatibility test for the installed model and adapter.
- A barge-in test that discards queued output.
- A disconnect test that cancels the agent task.
- An idempotent cleanup test.
- An opt-in live IVS audio traversal check.

Use the complete [Strands sample](../../../strands/) for lifecycle and test patterns.

