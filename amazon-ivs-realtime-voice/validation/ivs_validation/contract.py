from __future__ import annotations

import hashlib
import importlib
import importlib.util
import math
import sys
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, runtime_checkable


class VoiceContractError(Exception):
    """Base error raised at the normalised adapter boundary."""


class UnsupportedPcmFormat(VoiceContractError, ValueError):
    """The adapter received PCM outside the agreed ingress format."""


class MalformedPcm(VoiceContractError, ValueError):
    """The PCM payload ends part-way through a sample."""


class ParticipantNotConnected(VoiceContractError, RuntimeError):
    """An operation targeted a participant without an active session."""


@dataclass(frozen=True, slots=True)
class PcmFormat:
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate_hz * self.channels * self.sample_width_bytes

    def bytes_for_ms(self, duration_ms: int) -> int:
        numerator = self.bytes_per_second * duration_ms
        if numerator % 1_000:
            raise ValueError(f"{duration_ms} ms is not sample-aligned for {self.sample_rate_hz} Hz")
        return numerator // 1_000


@dataclass(frozen=True, slots=True)
class PcmFrame:
    data: bytes
    format: PcmFormat

    @property
    def duration_seconds(self) -> float:
        return len(self.data) / self.format.bytes_per_second


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    kind: str
    participant_id: str
    at_seconds: float
    frame: PcmFrame | None = None


@dataclass(frozen=True, slots=True)
class OutputEvent:
    participant_id: str
    frame: PcmFrame
    at_seconds: float


@dataclass(frozen=True, slots=True)
class HandshakeEvidence:
    adapter_name: str
    format: PcmFormat
    session_opened: bool
    ingress_accepted: bool
    session_closed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LiveLoopbackEvidence:
    adapter_name: str
    transmitted: PcmFrame
    received: PcmFrame
    round_trip_seconds: float
    detail: str = ""


class ManualClock:
    def __init__(self) -> None:
        self._now_seconds = 0.0

    def now(self) -> float:
        return self._now_seconds

    def advance_ms(self, duration_ms: int) -> None:
        if duration_ms < 0:
            raise ValueError("clock cannot move backwards")
        self._now_seconds += duration_ms / 1_000


class RecordingProvider:
    """Provider test double supplied to each adapter factory."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.events: list[ProviderEvent] = []

    def open(self, participant_id: str) -> None:
        self._record("open", participant_id)

    def send_pcm(self, participant_id: str, frame: PcmFrame) -> None:
        self._record("ingress", participant_id, frame)

    def cancel(self, participant_id: str) -> None:
        self._record("cancel", participant_id)

    def close(self, participant_id: str) -> None:
        self._record("close", participant_id)

    def kinds(self, participant_id: str) -> list[str]:
        return [event.kind for event in self.events if event.participant_id == participant_id]

    def of_kind(self, kind: str, participant_id: str) -> list[ProviderEvent]:
        return [
            event
            for event in self.events
            if event.kind == kind and event.participant_id == participant_id
        ]

    def _record(
        self,
        kind: str,
        participant_id: str,
        frame: PcmFrame | None = None,
    ) -> None:
        self.events.append(
            ProviderEvent(
                kind=kind,
                participant_id=participant_id,
                frame=frame,
                at_seconds=self.clock.now(),
            )
        )


class RecordingOutput:
    """IVS output test double supplied to each adapter factory."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.events: list[OutputEvent] = []

    def write_pcm(self, participant_id: str, frame: PcmFrame) -> None:
        self.events.append(
            OutputEvent(
                participant_id=participant_id,
                frame=frame,
                at_seconds=self.clock.now(),
            )
        )

    def for_participant(self, participant_id: str) -> list[OutputEvent]:
        return [event for event in self.events if event.participant_id == participant_id]


@dataclass(slots=True)
class ContractEnvironment:
    clock: ManualClock
    provider: RecordingProvider
    output: RecordingOutput
    ingress_format: PcmFormat
    output_frame_ms: int = 20


@runtime_checkable
class VoiceAdapterDriver(Protocol):
    """Normalised boundary implemented by a thin shim around each sample."""

    def connect(self, participant_id: str) -> None: ...

    def receive_ingress(self, participant_id: str, frame: PcmFrame) -> None: ...

    def receive_provider_audio(
        self,
        participant_id: str,
        frame: PcmFrame,
    ) -> None: ...

    def pump(self) -> None: ...

    def interrupt(self, participant_id: str) -> None: ...

    def disconnect(self, participant_id: str) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class AdapterCase:
    name: str
    driver: VoiceAdapterDriver
    clock: ManualClock
    provider: RecordingProvider
    output: RecordingOutput
    metadata: dict[str, Any] = field(default_factory=dict)


AdapterFactory = Callable[[ContractEnvironment], VoiceAdapterDriver]


def assert_pcm16_mono(
    frame: PcmFrame,
    *,
    expected_sample_rate_hz: int,
) -> None:
    expected = PcmFormat(
        sample_rate_hz=expected_sample_rate_hz,
        channels=1,
        sample_width_bytes=2,
    )
    if frame.format != expected:
        raise UnsupportedPcmFormat(
            f"expected {expected_sample_rate_hz} Hz 16-bit mono PCM, got {frame.format}"
        )
    if len(frame.data) % frame.format.sample_width_bytes:
        raise MalformedPcm("PCM payload contains a partial sample")


def make_pcm_ramp(
    *,
    duration_ms: int,
    pcm_format: PcmFormat,
    start_sample: int = 0,
) -> PcmFrame:
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    byte_count = pcm_format.bytes_for_ms(duration_ms)
    sample_count = byte_count // pcm_format.sample_width_bytes
    samples = (((start_sample + index + 32_768) % 65_536) - 32_768 for index in range(sample_count))
    data = b"".join(sample.to_bytes(2, byteorder="little", signed=True) for sample in samples)
    return PcmFrame(data=data, format=pcm_format)


def make_loopback_marker(
    *,
    marker_id: str,
    duration_ms: int,
    pcm_format: PcmFormat,
) -> PcmFrame:
    if pcm_format.channels != 1 or pcm_format.sample_width_bytes != 2:
        raise UnsupportedPcmFormat("loopback markers require 16-bit mono PCM")
    if duration_ms < 80:
        raise ValueError("loopback marker must be at least 80 ms")

    sample_count = pcm_format.bytes_for_ms(duration_ms) // 2
    hop_samples = max(1, pcm_format.sample_rate_hz // 50)
    digest = hashlib.sha256(marker_id.encode("utf-8")).digest()
    frequencies = [430 + (digest[index] % 12) * 73 for index in range(min(16, len(digest)))]
    samples: list[bytes] = []
    phase = 0.0
    for index in range(sample_count):
        frequency = frequencies[(index // hop_samples) % len(frequencies)]
        phase += 2 * math.pi * frequency / pcm_format.sample_rate_hz
        sample = int(12_000 * math.sin(phase))
        samples.append(sample.to_bytes(2, byteorder="little", signed=True))
    return PcmFrame(data=b"".join(samples), format=pcm_format)


def marker_similarity(marker: PcmFrame, received: PcmFrame) -> float:
    if marker.format != received.format:
        return 0.0
    if not marker.data or len(received.data) < len(marker.data):
        return 0.0
    if marker.data in received.data:
        return 1.0

    marker_samples = _pcm16_samples(marker.data)
    received_samples = _pcm16_samples(received.data)
    marker_length = len(marker_samples)
    sample_stride = max(1, marker_length // 600)
    sampled_indices = range(0, marker_length, sample_stride)
    marker_energy = sum(marker_samples[index] ** 2 for index in sampled_indices)
    if marker_energy == 0:
        return 0.0

    best = 0.0
    for offset in range(len(received_samples) - marker_length + 1):
        dot = 0
        received_energy = 0
        for index in sampled_indices:
            received_value = received_samples[offset + index]
            dot += marker_samples[index] * received_value
            received_energy += received_value**2
        if received_energy:
            similarity = dot / math.sqrt(marker_energy * received_energy)
            best = max(best, similarity)
    return best


def load_factory(spec: str) -> AdapterFactory:
    target, separator, attribute = spec.rpartition(":")
    if not separator or not target or not attribute:
        raise ValueError("factory must use 'module:callable' or '/path/to/file.py:callable'")

    module = _load_module(target)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise TypeError(f"{spec!r} does not resolve to a callable")
    return factory


def _load_module(target: str) -> ModuleType:
    path = Path(target)
    if path.suffix == ".py" or path.exists():
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        module_name = f"_ivs_contract_{hashlib.sha256(str(resolved).encode()).hexdigest()[:12]}"
        module_spec = importlib.util.spec_from_file_location(module_name, resolved)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"cannot load adapter factory from {resolved}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        try:
            module_spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module
    return importlib.import_module(target)


def _pcm16_samples(data: bytes) -> array[int]:
    samples = array("h")
    samples.frombytes(data)
    if samples.itemsize != 2:
        raise RuntimeError("platform does not expose 16-bit signed short samples")
    if sys.byteorder != "little":
        samples.byteswap()
    return samples
