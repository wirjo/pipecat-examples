from __future__ import annotations

import math

import pytest
from ivs_validation.contract import (
    MalformedPcm,
    PcmFormat,
    PcmFrame,
    UnsupportedPcmFormat,
    assert_pcm16_mono,
    load_factory,
    make_loopback_marker,
    make_pcm_ramp,
    marker_similarity,
)

PCM16_MONO_16KHZ = PcmFormat(
    sample_rate_hz=16_000,
    channels=1,
    sample_width_bytes=2,
)


def test_make_pcm_ramp_returns_exact_duration_of_pcm16_mono() -> None:
    frame = make_pcm_ramp(duration_ms=25, pcm_format=PCM16_MONO_16KHZ)

    assert frame.format == PCM16_MONO_16KHZ
    assert len(frame.data) == 800
    assert frame.duration_seconds == pytest.approx(0.025)


@pytest.mark.parametrize(
    "pcm_format",
    [
        PcmFormat(sample_rate_hz=16_000, channels=2, sample_width_bytes=2),
        PcmFormat(sample_rate_hz=16_000, channels=1, sample_width_bytes=1),
        PcmFormat(sample_rate_hz=8_000, channels=1, sample_width_bytes=2),
    ],
)
def test_assert_pcm16_mono_rejects_unsupported_metadata(
    pcm_format: PcmFormat,
) -> None:
    frame = PcmFrame(data=b"\x00\x00", format=pcm_format)

    with pytest.raises(UnsupportedPcmFormat):
        assert_pcm16_mono(frame, expected_sample_rate_hz=16_000)


def test_assert_pcm16_mono_rejects_partial_sample() -> None:
    frame = PcmFrame(data=b"\x00", format=PCM16_MONO_16KHZ)

    with pytest.raises(MalformedPcm):
        assert_pcm16_mono(frame, expected_sample_rate_hz=16_000)


def test_loopback_marker_is_detectable_after_leading_and_trailing_silence() -> None:
    marker = make_loopback_marker(
        marker_id="adapter-a",
        duration_ms=240,
        pcm_format=PCM16_MONO_16KHZ,
    )
    silence = b"\x00\x00" * 400
    received = PcmFrame(
        data=silence + marker.data + silence,
        format=PCM16_MONO_16KHZ,
    )

    assert marker_similarity(marker, received) >= 0.99


def test_loopback_marker_does_not_match_an_unrelated_tone() -> None:
    marker = make_loopback_marker(
        marker_id="adapter-a",
        duration_ms=240,
        pcm_format=PCM16_MONO_16KHZ,
    )
    sample_count = len(marker.data) // 2
    unrelated_samples = (
        int(12_000 * math.sin(2 * math.pi * 997 * index / 16_000)) for index in range(sample_count)
    )
    unrelated = PcmFrame(
        data=b"".join(sample.to_bytes(2, "little", signed=True) for sample in unrelated_samples),
        format=PCM16_MONO_16KHZ,
    )

    assert marker_similarity(marker, unrelated) < 0.5


def test_load_factory_supports_file_modules_with_postponed_dataclass_annotations(
    tmp_path,
) -> None:
    factory_file = tmp_path / "adapter_factory.py"
    factory_file.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from dataclasses import dataclass",
                "",
                "@dataclass",
                "class Driver:",
                "    environment: ContractEnvironment",
                "",
                "def create_driver(environment):",
                "    return Driver(environment)",
            ]
        ),
        encoding="utf-8",
    )
    environment = object()

    factory = load_factory(f"{factory_file}:create_driver")

    assert factory(environment).environment is environment
