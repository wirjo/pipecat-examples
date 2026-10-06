# Sample validation evidence

This directory records evidence for a validated, non-production sample.

The live runs used pre-existing Amazon IVS Real-Time stages and participants on
6 October 2026. No participant token, transcript text, service response, AWS
account identifier, or customer data is stored here.

The observer counted a frame as non-silent when its root mean square amplitude
exceeded `100`.

| Check | Recorded result |
|---|---|
| Direct IVS loopback | **100/100 non-silent frames** |
| Pipecat end-to-end | **86/900 non-silent frames** |
| Strands end-to-end | **260/750 non-silent frames** |
| Shared local tests | **12 passed** |
| Pipecat local tests | **8 passed** |
| Strands local tests | **14 passed** |
| Validation contract self-check | **15 passed, 2 opt-in tests skipped** |

The twelve shared tests include accepted default and explicit `443` ports, plus
rejection of malformed and nonstandard ports.

The live results prove only that non-silent audio returned through each selected
path. They do not measure audio continuity, transcription accuracy, response
quality, end-to-end latency, concurrency, load behaviour, or production
reliability.

The validation result covers the reference driver only. Pipecat and Strands
contract drivers were not included or run. The opt-in provider handshake and
live marker checks were skipped.

Only the services and models named in the recorded paths were validated.
Cartesia and third-party speech models hosted on Amazon SageMaker were not
tested.

See [`results.json`](results.json) for the recorded results and explicit limits.
