# IVS voice sample contract

This directory defines a provider-neutral behavioural contract for comparing
voice adapters. It is a test harness, not production code.

The completed self-check uses only
[`tests/reference_adapter.py`](tests/reference_adapter.py). This repository
does not contain Pipecat or Strands contract drivers, so the contract has not
validated either implementation.

## Run the contract self-check

Run this command from the repository root:

```console
uv --directory amazon-ivs-realtime-voice/validation run --no-project --python 3.12 --with-requirements requirements-test.txt python -m pytest --adapter-factory tests/reference_adapter.py:create_driver
```

The recorded result is 15 passed and 2 skipped. The provider handshake and live
IVS checks skip because they require explicit opt-in configuration.

## Implement an adapter driver

Implement the methods defined by `VoiceAdapterDriver` in
[`ivs_validation/contract.py`](ivs_validation/contract.py). Expose a factory
that accepts `ContractEnvironment` and returns the driver.

Wire the injected manual clock, recording provider, and recording output into
the adapter. Do not use production dependencies for the required offline
level. If the driver wraps async code, make `pump()` expose all work due at the
manual clock's current time before returning.

Pass one `--adapter-factory` option per driver. Each value must use
`module:callable` or `path/to/file.py:callable` syntax. The
`IVS_VOICE_ADAPTER_FACTORIES` environment variable accepts the same values as a
comma-separated list.

Add `--provider-handshake` or `--live-ivs` only when each driver implements the
matching method and points at pre-existing test infrastructure. Neither option
creates AWS resources.

See [`TEST_PLAN.md`](TEST_PLAN.md) for the required behaviours and evidence
levels.
