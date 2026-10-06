"""Small conforming driver used to prove the contract itself is runnable."""

from __future__ import annotations

from dataclasses import dataclass, field

from ivs_validation.contract import (
    ContractEnvironment,
    ParticipantNotConnected,
    PcmFrame,
    assert_pcm16_mono,
)


@dataclass
class _ParticipantState:
    buffer: bytearray = field(default_factory=bytearray)
    next_emit_at: float | None = None


class ReferenceDriver:
    def __init__(self, environment: ContractEnvironment) -> None:
        self.environment = environment
        self.participants: dict[str, _ParticipantState] = {}

    def connect(self, participant_id: str) -> None:
        if participant_id in self.participants:
            return
        self.participants[participant_id] = _ParticipantState()
        self.environment.provider.open(participant_id)

    def receive_ingress(self, participant_id: str, frame: PcmFrame) -> None:
        self._state(participant_id)
        self._validate(frame)
        self.environment.provider.send_pcm(participant_id, frame)

    def receive_provider_audio(
        self,
        participant_id: str,
        frame: PcmFrame,
    ) -> None:
        state = self._state(participant_id)
        self._validate(frame)
        state.buffer.extend(frame.data)

    def pump(self) -> None:
        now = self.environment.clock.now()
        frame_bytes = self.environment.ingress_format.bytes_for_ms(self.environment.output_frame_ms)
        frame_seconds = self.environment.output_frame_ms / 1_000
        for participant_id, state in self.participants.items():
            if len(state.buffer) < frame_bytes:
                continue
            if state.next_emit_at is not None and now < state.next_emit_at:
                continue

            payload = bytes(state.buffer[:frame_bytes])
            del state.buffer[:frame_bytes]
            self.environment.output.write_pcm(
                participant_id,
                PcmFrame(data=payload, format=self.environment.ingress_format),
            )
            state.next_emit_at = (
                now + frame_seconds
                if state.next_emit_at is None
                else state.next_emit_at + frame_seconds
            )

    def interrupt(self, participant_id: str) -> None:
        state = self._state(participant_id)
        state.buffer.clear()
        state.next_emit_at = None
        self.environment.provider.cancel(participant_id)

    def disconnect(self, participant_id: str) -> None:
        state = self.participants.pop(participant_id, None)
        if state is None:
            return
        state.buffer.clear()
        self.environment.provider.close(participant_id)

    def close(self) -> None:
        for participant_id in list(self.participants):
            self.disconnect(participant_id)

    def _state(self, participant_id: str) -> _ParticipantState:
        try:
            return self.participants[participant_id]
        except KeyError as error:
            raise ParticipantNotConnected(participant_id) from error

    def _validate(self, frame: PcmFrame) -> None:
        assert_pcm16_mono(
            frame,
            expected_sample_rate_hz=self.environment.ingress_format.sample_rate_hz,
        )


def create_driver(environment: ContractEnvironment) -> ReferenceDriver:
    return ReferenceDriver(environment)
