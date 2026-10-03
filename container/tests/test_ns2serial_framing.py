#!/usr/bin/env python3
"""The portable framing contract (§2.2, ADR-0006), without a port.

What matters here is not that a round trip works but that the *failure* modes
are the specified ones: a corrupted frame costs one frame (never the link), a
log line costs one dropped block, an oversize block is discarded silently, and
the CRC covers header bytes 0–4 plus the payload.

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2serial*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from container.ns2device.model import FrameType, MAX_FRAME, Verb  # noqa: E402
from container.ns2serial import (  # noqa: E402
    Frame,
    FrameDecoder,
    cobs_decode,
    cobs_encode,
    crc16_ccitt_false,
    decode_frame,
    encode_request,
)


class Cobs(unittest.TestCase):
    def test_round_trip_with_zeros_and_long_runs(self):
        cases = [
            b"",
            b"\x00",
            b"\x00\x00\x00",
            b"\x01\x00\x02\x00\x03",
            bytes(range(1, 255)),
            bytes(range(256)) * 3,
        ]
        for data in cases:
            with self.subTest(n=len(data)):
                encoded = cobs_encode(data)
                self.assertNotIn(0x00, encoded)
                self.assertEqual(cobs_decode(encoded), data)

    def test_decode_rejects_a_zero_code_byte(self):
        with self.assertRaises(ValueError):
            cobs_decode(b"\x00")


class Crc(unittest.TestCase):
    def test_ccitt_false_known_vector(self):
        # CRC-16/CCITT-FALSE of "123456789" is 0x29B1.
        self.assertEqual(crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_two_segments_equal_the_concatenation(self):
        head, payload = b"\x01\x01\x01\x05\x00", b"hello"
        self.assertEqual(crc16_ccitt_false(head, payload), crc16_ccitt_false(head + payload))


class Frames(unittest.TestCase):
    def test_a_request_round_trips(self):
        wire = encode_request(int(Verb.HELLO), b"\x01")
        self.assertEqual(wire[0], 0x00)
        self.assertEqual(wire[-1], 0x00)
        frames = list(FrameDecoder().feed(wire))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], Frame(1, int(FrameType.REQUEST), int(Verb.HELLO), b"\x01"))

    def test_a_flipped_byte_costs_one_frame_not_the_link(self):
        good = encode_request(int(Verb.STATUS))
        # Flip a payload/CRC byte inside the COBS block, past the leading 0x00.
        corrupted = bytearray(good)
        corrupted[3] ^= 0xFF
        decoder = FrameDecoder()
        self.assertEqual(list(decoder.feed(bytes(corrupted))), [])
        self.assertEqual(decoder.dropped, 1)
        # The next well-formed frame after a delimiter is still decoded.
        self.assertEqual(len(list(decoder.feed(good))), 1)

    def test_a_log_line_is_dropped_and_two_frames_survive(self):
        wire = encode_request(int(Verb.HELLO), b"\x01")
        noise = b"I (1288) hid: report task started, itvl=4\n"
        decoder = FrameDecoder()
        frames = list(decoder.feed(noise + wire + wire))
        self.assertEqual(len(frames), 2)
        self.assertEqual(decoder.dropped, 1)

    def test_empty_segments_are_ignored(self):
        wire = encode_request(int(Verb.STATUS))
        decoder = FrameDecoder()
        self.assertEqual(len(list(decoder.feed(b"\x00\x00" + wire + b"\x00\x00"))), 1)

    def test_an_oversize_block_is_discarded(self):
        big = encode_request(int(Verb.LOAD_PLAN), b"\x01" * (MAX_FRAME + 10))
        decoder = FrameDecoder()
        self.assertEqual(list(decoder.feed(big)), [])
        self.assertEqual(decoder.dropped, 1)

    def test_a_partial_frame_waits_for_the_rest(self):
        wire = encode_request(int(Verb.HELLO), b"\x01")
        decoder = FrameDecoder()
        self.assertEqual(list(decoder.feed(wire[:4])), [])
        self.assertEqual(len(list(decoder.feed(wire[4:]))), 1)

    def test_decode_frame_rejects_a_length_disagreement(self):
        wire = encode_request(int(Verb.HELLO), b"\x01")
        decoded = bytearray(cobs_decode(wire[1:-1]))
        decoded[3] = 2  # len says 2, payload is 1 byte
        self.assertIsNone(decode_frame(bytes(decoded)))


if __name__ == "__main__":
    unittest.main()
