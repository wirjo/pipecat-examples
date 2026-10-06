"""Offline tests for IVS token handling."""

from __future__ import annotations

import base64
import json
import unittest

from aiortc import RTCPeerConnection
from ivs_strands.adapters import IVSOutputAudioTrack, IVSOutputStream
from ivs_strands.ivs import (
    configure_ivs_publisher_media,
    fix_ivs_answer_sdp,
    parse_stage_token,
    require_ivs_url,
    require_stage_capabilities,
)


def synthetic_token(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class IVSTokenTests(unittest.TestCase):
    def test_ivs_url_accepts_service_hosts(self) -> None:
        for value in (
            "https://abc.live-video.net/session",
            "https://abc.live-video.net:443/session",
        ):
            with self.subTest(value=value):
                self.assertEqual(require_ivs_url(value), value)

    def test_ivs_url_rejects_untrusted_hosts(self) -> None:
        values = (
            "http://abc.live-video.net/session",
            "https://example.com/session",
            "https://live-video.net@example.com/session",
            "https://abc.live-video.net:8443/session",
            "https://abc.live-video.net:not-a-port/session",
        )

        for value in values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, "untrusted endpoint"):
                    require_ivs_url(value)

    def test_token_parse_and_capability_validation(self) -> None:
        payload = {
            "capabilities": {
                "allow_publish": True,
                "allow_subscribe": True,
            },
            "whip_url": "https://example.live-video.net",
        }
        parsed = parse_stage_token(synthetic_token(payload))

        self.assertEqual(parsed, payload)
        require_stage_capabilities(parsed)

    def test_missing_capability_is_rejected(self) -> None:
        payload = {
            "capabilities": {
                "allow_publish": True,
                "allow_subscribe": False,
            }
        }

        with self.assertRaisesRegex(ValueError, "subscribe"):
            require_stage_capabilities(payload)

    def test_sdp_candidates_are_copied_into_each_missing_media_section(self) -> None:
        candidate = "a=candidate:1 1 UDP 1 192.0.2.1 5000 typ host"
        answer = (
            "v=0\r\n"
            "a=group:BUNDLE 0 1 2\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
            "a=mid:0\r\n"
            f"{candidate}\r\n"
            "a=end-of-candidates\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 102\r\n"
            "a=mid:1\r\n"
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
            "a=mid:2\r\n"
        )

        fixed = fix_ivs_answer_sdp(answer)
        sections = {
            lines[0].split()[0]: lines
            for lines in (section.splitlines() for section in fixed.split("\r\nm=") if section)
        }

        self.assertEqual(fixed.count(candidate), 3)
        self.assertEqual(fixed.count("a=end-of-candidates"), 3)
        self.assertIn(candidate, sections["video"])
        self.assertIn(candidate, sections["application"])

    def test_sdp_without_candidates_is_unchanged(self) -> None:
        answer = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        self.assertEqual(fix_ivs_answer_sdp(answer), answer)


class IVSPublisherOfferTests(unittest.IsolatedAsyncioTestCase):
    async def test_publisher_offer_has_sendrecv_audio_and_h264_video(self) -> None:
        pc = RTCPeerConnection()
        output = IVSOutputStream()
        audio = IVSOutputAudioTrack(output)
        video = configure_ivs_publisher_media(pc, audio)

        try:
            offer = await pc.createOffer()
            transceivers = pc.getTransceivers()

            self.assertEqual([item.direction for item in transceivers], ["sendrecv", "sendrecv"])
            self.assertIs(transceivers[0].sender.track, audio)
            self.assertIs(transceivers[1].sender.track, video)
            self.assertIn("m=audio", offer.sdp)
            self.assertIn("m=video", offer.sdp)
            self.assertIn("H264/90000", offer.sdp)
            self.assertNotIn("VP8/90000", offer.sdp)
        finally:
            await pc.close()
