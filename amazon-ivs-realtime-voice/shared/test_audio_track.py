import asyncio

import pytest
from ivs_loopback import BufferedAudioTrack, require_ivs_url, safe_ivs_redirect


@pytest.mark.asyncio
async def test_interrupt_clears_buffer() -> None:
    track = BufferedAudioTrack(sample_rate=24_000)
    await track.add_audio(b"\x01\x00" * 2_400)
    assert await track.buffered_bytes() == 4_800

    cleared = await track.interrupt()

    assert cleared == 4_800
    assert await track.buffered_bytes() == 0


@pytest.mark.asyncio
async def test_recv_paces_twenty_millisecond_frames() -> None:
    track = BufferedAudioTrack(sample_rate=24_000)
    await track.add_audio(b"\x01\x00" * 960)

    started = asyncio.get_running_loop().time()
    frame = await track.recv()
    elapsed = asyncio.get_running_loop().time() - started

    assert frame.sample_rate == 24_000
    assert frame.samples == 480
    assert elapsed >= 0.015


def test_safe_ivs_redirect_accepts_service_hosts() -> None:
    assert (
        safe_ivs_redirect(
            "https://global.whip.live-video.net",
            "https://abc.live-video.net/session",
        )
        == "https://abc.live-video.net/session"
    )


def test_require_ivs_url_accepts_service_hosts() -> None:
    for value in (
        "https://abc.live-video.net/session",
        "https://abc.live-video.net:443/session",
    ):
        assert require_ivs_url(value) == value


@pytest.mark.parametrize(
    "url",
    [
        "http://abc.live-video.net/session",
        "https://example.com/session",
        "https://live-video.net@example.com/session",
        "https://abc.live-video.net:8443/session",
        "https://abc.live-video.net:not-a-port/session",
    ],
)
def test_require_ivs_url_rejects_untrusted_hosts(url: str) -> None:
    with pytest.raises(RuntimeError, match="untrusted destination"):
        require_ivs_url(url)


@pytest.mark.parametrize(
    "location",
    [
        "http://abc.live-video.net/session",
        "https://example.com/session",
        "//example.com/session",
    ],
)
def test_safe_ivs_redirect_rejects_untrusted_hosts(location: str) -> None:
    with pytest.raises(RuntimeError, match="untrusted destination"):
        safe_ivs_redirect("https://global.whip.live-video.net", location)
