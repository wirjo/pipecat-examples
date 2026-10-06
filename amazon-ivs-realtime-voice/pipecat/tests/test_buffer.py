from amazon_ivs_pipecat.buffer import PCM16PlayoutBuffer


async def test_buffer_drops_oldest_audio_on_overflow() -> None:
    buffer = PCM16PlayoutBuffer(
        sample_rate=1_000,
        num_channels=1,
        frame_duration_ms=20,
        max_buffer_ms=40,
    )
    chunk = buffer.chunk_bytes
    first = bytes([1]) * chunk
    second = bytes([2]) * chunk
    third = bytes([3]) * chunk

    assert await buffer.append(first + second + third)

    assert buffer.buffered_bytes == chunk * 2
    assert buffer.dropped_bytes == chunk
    assert await buffer.pop_chunk() == second
    assert await buffer.pop_chunk() == third


async def test_buffer_pads_short_read_with_silence() -> None:
    buffer = PCM16PlayoutBuffer(
        sample_rate=1_000,
        num_channels=1,
        frame_duration_ms=20,
        max_buffer_ms=40,
    )
    partial = b"\x01\x02" * 5
    await buffer.append(partial)

    chunk = await buffer.pop_chunk()

    assert chunk[: len(partial)] == partial
    assert chunk[len(partial) :] == bytes(buffer.chunk_bytes - len(partial))
    assert buffer.buffered_bytes == 0
