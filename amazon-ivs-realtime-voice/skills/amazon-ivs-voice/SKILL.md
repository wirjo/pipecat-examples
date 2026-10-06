---
name: amazon-ivs-voice
description: Wrap an existing Pipecat or Strands voice agent with Amazon IVS Real-Time WebRTC. Use this skill whenever a developer asks to connect, expose, migrate, or add browser voice streaming to a Pipecat pipeline, Strands BidiAgent, or Pipecat agent that uses StrandsAgentsProcessor.
compatibility: Requires an installed Pipecat AmazonIVSTransport or merged Strands Amazon IVS media adapter, plus a trusted backend that vends short-lived IVS participant credentials.
---

# Amazon IVS voice-agent wrapper

Add Amazon IVS as the WebRTC media boundary around an existing voice agent. Preserve the
agent's models, tools, prompts, processors, and business logic.

Use the [Pipecat sample](../../pipecat/) and [Strands sample](../../strands/) as reference
implementations. Do not copy their signalling or media internals into the target application.

## Assumptions

- Pipecat exports `AmazonIVSTransport` and `AmazonIVSParams` from
  `pipecat.transports.amazon_ivs`.
- Strands provides an installed Amazon IVS media adapter with equivalent input stream,
  output stream, publication track, and connection lifecycle APIs.
- A trusted backend creates or selects the IVS stage and returns short-lived participant
  credentials.

If the required transport or adapter is unavailable in the installed dependency, stop and
report the missing package version or merge requirement. Do not rebuild `aiortc`, WHIP, WHEP,
SDP repair, codec negotiation, or PCM buffering inside the application.

## Workflow

1. Inspect the existing agent before changing files.

   Identify its framework, entry point, dependency manager, pipeline construction, shutdown
   path, and tests. Search for `Pipeline`, `BaseTransport`, `BidiAgent`,
   `StrandsAgentsProcessor`, `agent.run`, and existing WebSocket or WebRTC transports.

2. Select one integration path.

   - Use the Pipecat path for a Pipecat pipeline, including a pipeline whose reasoning
     processor is `StrandsAgentsProcessor`.
   - Use the Strands path for a native Strands `BidiAgent` with streaming audio inputs and
     outputs.
   - If both patterns exist, ask which entry point to wrap. Do not silently change both.

3. Preserve the existing voice-agent behaviour.

   Replace or add only the media boundary. Keep the current STT, language model, TTS,
   speech-to-speech model, tools, context aggregators, turn detection, prompts, and
   observability unless the user asks to change them.

4. Define the trusted-backend session contract.

   Obtain these values at runtime:

   - An opaque agent participant token with `PUBLISH` and `SUBSCRIBE`.
   - The source participant ID whose audio enters the agent.
   - The full WHEP subscription URL for that source participant.
   - The stage identifier needed by the browser token flow.

   Keep AWS credentials and provider API keys on the server. Do not decode, log, persist, or
   send the agent participant token or WHEP URL to browser code. Give the browser only its own
   short-lived participant token and capabilities.

5. Apply the framework-specific wrapper.

   - For Pipecat, read [references/pipecat.md](references/pipecat.md).
   - For native Strands bidirectional audio, read
     [references/strands.md](references/strands.md).

6. Connect interruption and lifecycle handling.

   Preserve barge-in behaviour. Clear queued, interruptible audio when the agent receives an
   interruption. On cancellation, disconnect, or input-track end, stop model streams, media
   streams, publication tracks, worker tasks, and peer connections.

7. Add validation at the media boundary.

   Add provider-free tests for configuration, pipeline order, interruption cleanup, and
   idempotent shutdown. Add an opt-in live test that sends known PCM audio through IVS and
   confirms that non-silent audio returns. Treat audio traversal as connectivity evidence,
   not a speech-quality or latency result.

8. Document how to run and clean up the wrapper.

   Add exact install, test, and start commands. Name required environment variables without
   adding secrets to `.env.example`. Explain how to expire temporary tokens and remove test
   stages only when no other participant uses them.

## Guardrails

- Accept only the public merged transport or adapter API. Do not vendor its source.
- Accept the WHEP URL only from a trusted backend and pass it to the transport as an opaque
  value.
- Do not construct a subscription URL from an untrusted browser request.
- Do not put AWS credentials, participant tokens, provider keys, transcripts, or customer
  audio in source control or logs.
- Keep Pipecat and native Strands dependency environments separate unless the target
  application already combines them and its lock file resolves successfully.
- Do not claim production readiness from a single live audio check.

## Required output

Return:

1. The detected framework and selected integration path.
2. The files changed and the agent behaviour preserved.
3. The trusted-backend session fields the application expects.
4. Exact install, test, run, and cleanup commands.
5. Test results and limits.
6. Any unresolved package API, authentication, browser client, or deployment requirement.

