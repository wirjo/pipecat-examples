# Amazon IVS voice adapter validation contract

## Scope

Use this contract to compare adapter behaviour through thin synchronous driver
shims. The required checks use 16 kHz, signed 16-bit, little-endian mono PCM
and 20 ms output frames.

This contract is reference test infrastructure. Passing it would not make an
adapter production-ready.

| Behaviour | Contract evidence |
|---|---|
| PCM ingress | Exact bytes and format reach the injected provider; stereo, 8-bit, and partial samples fail before a provider call. |
| Output buffering | A 7 ms chunk emits nothing; a following 13 ms chunk emits one complete 20 ms frame with no byte loss or reordering. |
| Output pacing | At most one complete frame emits per 20 ms clock interval. |
| User interruption | The provider receives one cancel event and all queued output is discarded. |
| Disconnect cleanup | Provider state closes once, queued output is discarded, and another participant continues unaffected. |
| Live marker | An opt-in, deterministic PCM marker returns through an existing IVS path with correlation of at least 0.8. |

## Current evidence status

| Target | Status |
|---|---|
| Contract reference driver | **15 passed, 2 opt-in tests skipped** |
| Pipecat contract driver | **Not included or run** |
| Strands contract driver | **Not included or run** |
| Shared local suite | **12 passed** |
| Pipecat local suite | **8 passed** |
| Strands local suite | **14 passed** |

The separate live sample checks observed 100/100 non-silent frames for direct
IVS loopback, 86/900 for Pipecat, and 260/750 for Strands. Those checks used an
RMS threshold greater than `100`. They did not run the contract's correlation
or round-trip-time assertions.

## Evidence levels

1. **Offline - required for each adapter.** Uses only the manual clock,
   recording provider, and recording IVS output supplied by this contract.
   Network access, AWS credentials, secrets, and AWS resources are not used.
2. **Provider handshake - opt-in.** The driver exposes
   `provider_handshake(...)` and returns structured evidence that a real
   provider session opened, accepted PCM, and closed. The contract contains no
   provider SDK or credentials; the sample injects its configured client.
3. **Live end-to-end - opt-in.** The driver exposes `live_loopback(...)` and
   uses a pre-existing IVS test path. The test checks marker correlation and
   round-trip time. It never provisions or deletes AWS resources.

Pass level 1 before comparing the samples. Record levels 2 and 3 separately
because external service health and configuration can fail independently of
adapter behaviour.

## Evidence rules

- Record the exact command, date, driver revision, and result counts.
- Store no participant tokens, credentials, transcript text, account
  identifiers, customer data, or raw provider responses.
- Record provider handshake, live IVS, and offline results separately.
- State what each result does not establish, including latency, concurrency,
  load behaviour, and production reliability.
