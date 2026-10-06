from __future__ import annotations

import pytest
from ivs_validation.contract import (
    HandshakeEvidence,
    LiveLoopbackEvidence,
    MalformedPcm,
    ParticipantNotConnected,
    PcmFormat,
    PcmFrame,
    UnsupportedPcmFormat,
    make_loopback_marker,
    make_pcm_ramp,
    marker_similarity,
)

PCM16_MONO_16KHZ = PcmFormat(
    sample_rate_hz=16_000,
    channels=1,
    sample_width_bytes=2,
)
FRAME_MS = 20


def test_pcm16_mono_ingress_reaches_injected_provider_unchanged(adapter_case) -> None:
    participant_id = "participant-a"
    frame = make_pcm_ramp(duration_ms=40, pcm_format=PCM16_MONO_16KHZ)

    adapter_case.driver.connect(participant_id)
    adapter_case.driver.receive_ingress(participant_id, frame)

    assert adapter_case.provider.kinds(participant_id) == ["open", "ingress"]
    ingress = adapter_case.provider.of_kind("ingress", participant_id)
    assert [event.frame for event in ingress] == [frame]


@pytest.mark.parametrize(
    ("frame", "error"),
    [
        (
            PcmFrame(
                data=b"\x00\x00",
                format=PcmFormat(16_000, channels=2, sample_width_bytes=2),
            ),
            UnsupportedPcmFormat,
        ),
        (
            PcmFrame(
                data=b"\x00\x00",
                format=PcmFormat(16_000, channels=1, sample_width_bytes=1),
            ),
            UnsupportedPcmFormat,
        ),
        (
            PcmFrame(data=b"\x00", format=PCM16_MONO_16KHZ),
            MalformedPcm,
        ),
    ],
)
def test_ingress_rejects_non_pcm16_mono_or_partial_samples(
    adapter_case,
    frame: PcmFrame,
    error: type[Exception],
) -> None:
    adapter_case.driver.connect("participant-a")

    with pytest.raises(error):
        adapter_case.driver.receive_ingress("participant-a", frame)

    assert adapter_case.provider.of_kind("ingress", "participant-a") == []


def test_output_buffers_partial_frames_and_paces_complete_frames(adapter_case) -> None:
    participant_id = "participant-a"
    first_7ms = make_pcm_ramp(
        duration_ms=7,
        pcm_format=PCM16_MONO_16KHZ,
        start_sample=1,
    )
    next_13ms = make_pcm_ramp(
        duration_ms=13,
        pcm_format=PCM16_MONO_16KHZ,
        start_sample=113,
    )
    final_40ms = make_pcm_ramp(
        duration_ms=40,
        pcm_format=PCM16_MONO_16KHZ,
        start_sample=321,
    )
    expected = first_7ms.data + next_13ms.data + final_40ms.data

    adapter_case.driver.connect(participant_id)
    adapter_case.driver.receive_provider_audio(participant_id, first_7ms)
    adapter_case.driver.pump()
    assert adapter_case.output.events == []

    adapter_case.driver.receive_provider_audio(participant_id, next_13ms)
    adapter_case.driver.pump()
    assert len(adapter_case.output.events) == 1

    adapter_case.driver.receive_provider_audio(participant_id, final_40ms)
    adapter_case.driver.pump()
    assert len(adapter_case.output.events) == 1

    adapter_case.clock.advance_ms(FRAME_MS - 1)
    adapter_case.driver.pump()
    assert len(adapter_case.output.events) == 1

    adapter_case.clock.advance_ms(1)
    adapter_case.driver.pump()
    adapter_case.clock.advance_ms(FRAME_MS)
    adapter_case.driver.pump()

    events = adapter_case.output.for_participant(participant_id)
    assert b"".join(event.frame.data for event in events) == expected
    assert [len(event.frame.data) for event in events] == [640, 640, 640]
    assert all(event.frame.format == PCM16_MONO_16KHZ for event in events)
    assert [event.at_seconds for event in events] == pytest.approx([0.0, 0.02, 0.04])


def test_user_interruption_cancels_provider_and_flushes_buffered_output(
    adapter_case,
) -> None:
    participant_id = "participant-a"
    response = make_pcm_ramp(duration_ms=80, pcm_format=PCM16_MONO_16KHZ)

    adapter_case.driver.connect(participant_id)
    adapter_case.driver.receive_provider_audio(participant_id, response)
    adapter_case.driver.pump()
    assert len(adapter_case.output.events) == 1

    adapter_case.driver.interrupt(participant_id)
    adapter_case.clock.advance_ms(1_000)
    adapter_case.driver.pump()

    assert adapter_case.provider.kinds(participant_id) == ["open", "cancel"]
    assert len(adapter_case.output.for_participant(participant_id)) == 1


def test_disconnect_cleans_one_participant_without_affecting_another(
    adapter_case,
) -> None:
    participant_a = "participant-a"
    participant_b = "participant-b"
    frame = make_pcm_ramp(duration_ms=20, pcm_format=PCM16_MONO_16KHZ)

    adapter_case.driver.connect(participant_a)
    adapter_case.driver.connect(participant_b)
    adapter_case.driver.receive_provider_audio(participant_a, frame)
    adapter_case.driver.receive_provider_audio(participant_b, frame)
    adapter_case.driver.disconnect(participant_a)
    adapter_case.driver.disconnect(participant_a)
    adapter_case.clock.advance_ms(1_000)
    adapter_case.driver.pump()

    assert adapter_case.provider.kinds(participant_a) == ["open", "close"]
    assert adapter_case.provider.kinds(participant_b) == ["open"]
    assert adapter_case.output.for_participant(participant_a) == []
    assert len(adapter_case.output.for_participant(participant_b)) == 1

    with pytest.raises(ParticipantNotConnected):
        adapter_case.driver.receive_ingress(participant_a, frame)

    adapter_case.driver.receive_ingress(participant_b, frame)
    assert adapter_case.provider.kinds(participant_b) == ["open", "ingress"]


@pytest.mark.provider_handshake
def test_provider_handshake_accepts_pcm16_mono(
    provider_handshake_case,
) -> None:
    frame = make_pcm_ramp(duration_ms=100, pcm_format=PCM16_MONO_16KHZ)

    evidence = provider_handshake_case.driver.provider_handshake(
        participant_id="provider-handshake",
        frame=frame,
        timeout_seconds=10,
    )

    assert isinstance(evidence, HandshakeEvidence)
    assert evidence.session_opened
    assert evidence.ingress_accepted
    assert evidence.session_closed
    assert evidence.format == PCM16_MONO_16KHZ


@pytest.mark.live_ivs
def test_live_ivs_loopback_contains_distinct_marker(live_ivs_case) -> None:
    marker = make_loopback_marker(
        marker_id=f"ivs-contract-{live_ivs_case.name}",
        duration_ms=300,
        pcm_format=PCM16_MONO_16KHZ,
    )

    evidence = live_ivs_case.driver.live_loopback(
        marker=marker,
        timeout_seconds=30,
    )

    assert isinstance(evidence, LiveLoopbackEvidence)
    assert evidence.transmitted == marker
    assert evidence.received.format == PCM16_MONO_16KHZ
    assert 0 <= evidence.round_trip_seconds <= 30
    assert marker_similarity(marker, evidence.received) >= 0.8
