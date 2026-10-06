# Strands sample

> [!WARNING]
> This path is validated as a sample, not as a production solution. It has not been
> validated for latency, scale, resilience, security, or operational support.

This package bridges one existing Amazon IVS Real-Time participant to Amazon
Nova 2 Sonic through the Strands bidirectional streaming API.

It creates no AWS resources. It joins a stage described by an existing
participant token and opens a transient Bedrock model stream.

## What it implements

- `IVSInputStream` waits for an aiortc audio track, resamples it to 16 kHz mono
  signed 16-bit PCM, and returns Strands `AudioDelta` objects.
- `IVSOutputStream` validates and decodes `BidiAudioDeltaEvent` chunks into a
  bounded PCM buffer.
- `IVSOutputAudioTrack` pulls 20 ms, 24 kHz mono PCM frames from that buffer and
  emits silence on underrun.
- `IVSBlankVideoTrack` publishes black `yuv420p` frames with H.264-only codec
  preferences because IVS WHIP rejects audio-only publisher offers.
- SDP repair copies bundled ICE candidates into media sections that lack them.
- `BidiBargeInEvent` immediately clears queued model speech.
- `BidiAgent.run()` starts and stops both adapters. The app closes peer
  connections and tracks in `finally`.

The package targets `strands-agents[bidi]==1.58.0` and `aiortc==1.15.0`. It
defaults to `amazon.nova-2-sonic-v1:0` in `us-east-1`. This directory ships a
committed `uv.lock`.

## Run the local checks

Use Python 3.12 or 3.13. Run these commands from the repository root. The
commands below use the validated Python 3.12 environment.

1. Sync the environment.

   ```console
   uv --directory amazon-ivs-realtime-voice/strands sync --python 3.12
   ```

2. Run the fourteen offline tests.

   ```console
   uv --directory amazon-ivs-realtime-voice/strands run python -m unittest discover -s tests -v
   ```

The offline suite covers PCM conversion, output buffering, silence padding,
barge-in, blank-video generation, H.264 negotiation, SDP repair, HTTPS IVS host
and port validation, and lifecycle cleanup.

## Optional Bedrock handshake

This command opens and closes a Nova 2 Sonic stream. Bedrock usage and normal
service charges may apply.

```console
uv --directory amazon-ivs-realtime-voice/strands run python -m ivs_strands --check-bedrock --region us-east-1
```

It uses the normal AWS credential chain and does not print credentials or
account identity. This optional handshake is not recorded in
`../evidence/results.json`.

## Run against an existing IVS stage

Run this path only in a test account. Use a pre-existing participant with
publish and subscribe capabilities for the agent. Pass the participant ID of
the existing audio source to `--subscribe-to`.

1. Read the token silently so its value does not enter shell history.

   ```console
   read -rs IVS_STAGE_TOKEN
   export IVS_STAGE_TOKEN
   ```

2. Run the agent from the repository root.

   ```console
   uv --directory amazon-ivs-realtime-voice/strands run python -m ivs_strands --subscribe-to existing-participant-id --region us-east-1 --model-id amazon.nova-2-sonic-v1:0 --voice tiffany
   ```

3. Remove the token from the environment after the run.

   ```console
   unset IVS_STAGE_TOKEN
   ```

The app does not log the token or decoded token identifiers. The command uses
the normal AWS credential chain for Bedrock access and may incur service
charges. `--log-level DEBUG` changes only the application logger; dependency
loggers remain at `WARNING`.

## Validated scope

The offline suite passed fourteen tests. The live path returned 260 non-silent
frames from 750 observed frames through:

```text
PCM prompt -> IVS -> Nova 2 Sonic -> IVS
```

The observer classified a frame as non-silent when its RMS amplitude exceeded
`100`. The 260/750 result proves only that non-silent audio returned through
the path. It does not measure audio continuity, response quality, end-to-end
latency, concurrency, load behaviour, or production reliability.

The provider-neutral validation contract has not run against this package
because no Strands contract driver is included.

## Constraints

- No application video or SEI metadata. The publisher still sends the blank
  H.264 track required by IVS WHIP.
- One subscribed participant per process.
- No acoustic echo cancellation. Use headphones during a live test.
- The output queue is capped at two seconds by default and drops oldest audio
  if downstream publishing stalls.
- No proactive model connection rotation or reconnect orchestration.
- No deployment, token service, monitoring, retry policy, load test, or
  operational runbook.
