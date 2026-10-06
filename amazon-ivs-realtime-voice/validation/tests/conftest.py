from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

VALIDATION_ROOT = Path(__file__).resolve().parents[1]
if str(VALIDATION_ROOT) not in sys.path:
    sys.path.insert(0, str(VALIDATION_ROOT))

from ivs_validation.contract import (  # noqa: E402
    AdapterCase,
    ContractEnvironment,
    ManualClock,
    PcmFormat,
    RecordingOutput,
    RecordingProvider,
    load_factory,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ivs-voice-contract")
    group.addoption(
        "--adapter-factory",
        action="append",
        default=[],
        metavar="MODULE:CALLABLE",
        help="Repeat once per adapter. File paths ending in .py are also accepted.",
    )
    group.addoption(
        "--provider-handshake",
        action="store_true",
        help="Run opt-in provider handshake checks exposed by each driver.",
    )
    group.addoption(
        "--live-ivs",
        action="store_true",
        help="Run opt-in live IVS marker loopback against pre-existing resources.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "provider_handshake: calls an explicitly configured provider handshake",
    )
    config.addinivalue_line(
        "markers",
        "live_ivs: calls an explicitly configured live IVS loopback",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    fixture_modes = {
        "adapter_case": None,
        "provider_handshake_case": "--provider-handshake",
        "live_ivs_case": "--live-ivs",
    }
    for fixture_name, required_option in fixture_modes.items():
        if fixture_name not in metafunc.fixturenames:
            continue

        enabled = required_option is None or metafunc.config.getoption(required_option)
        specs = _factory_specs(metafunc.config) if enabled else []
        if specs:
            parameters = [pytest.param(spec, id=_case_name(spec)) for spec in specs]
        else:
            reason = "no adapter factories configured" if enabled else f"requires {required_option}"
            parameters = [
                pytest.param(
                    None,
                    marks=pytest.mark.skip(reason=reason),
                    id="not-configured",
                )
            ]
        metafunc.parametrize(fixture_name, parameters, indirect=True)


@pytest.fixture
def adapter_case(request: pytest.FixtureRequest):
    yield from _build_case(request.param)


@pytest.fixture
def provider_handshake_case(request: pytest.FixtureRequest):
    yield from _build_case(request.param)


@pytest.fixture
def live_ivs_case(request: pytest.FixtureRequest):
    yield from _build_case(request.param)


def _build_case(factory_spec: str):
    clock = ManualClock()
    provider = RecordingProvider(clock)
    output = RecordingOutput(clock)
    ingress_format = PcmFormat(
        sample_rate_hz=16_000,
        channels=1,
        sample_width_bytes=2,
    )
    environment = ContractEnvironment(
        clock=clock,
        provider=provider,
        output=output,
        ingress_format=ingress_format,
    )
    factory = load_factory(factory_spec)
    driver = factory(environment)
    case = AdapterCase(
        name=_case_name(factory_spec),
        driver=driver,
        clock=clock,
        provider=provider,
        output=output,
        metadata={"factory_spec": factory_spec},
    )
    try:
        yield case
    finally:
        driver.close()


def _factory_specs(config: pytest.Config) -> list[str]:
    command_line_specs = config.getoption("--adapter-factory")
    environment_specs = [
        value.strip()
        for value in os.environ.get("IVS_VOICE_ADAPTER_FACTORIES", "").split(",")
        if value.strip()
    ]
    return list(dict.fromkeys([*command_line_specs, *environment_specs]))


def _case_name(spec: str) -> str:
    target, _, attribute = spec.rpartition(":")
    stem = Path(target).stem if target.endswith(".py") else target.rsplit(".", 1)[-1]
    return f"{stem}.{attribute}"
