"""COBS + CRC-16/CCITT-FALSE framing, as pure functions (§2.2, ADR-0006).

This is the serial seam's portable half: it owns no port, no asyncio and no
I/O, so it is the same code the host-side C tests mirror and the one place the
one framing for control and bulk is written. The exact contract:

```text
0x00 · COBS(ver ‖ type ‖ verb ‖ len(le16) ‖ crc(le16) ‖ payload) · 0x00
```

- The CRC is over **header bytes 0–4 then the payload** (`5 + len` bytes); the
  `crc` field's own two bytes are not covered, and neither are the COBS
  encoding nor the `0x00` delimiters.
- A frame whose decoded `7 + len` exceeds `max_frame` cannot have its CRC
  computed over the bytes the header claims, so it is dropped and the receiver
  advances to the next `0x00` (§2.8).
- A `0x00`-terminated segment that fails to decode, fails its CRC, or has a
  length disagreement is **discarded silently** — a receiver cannot name a code
  for bytes it could not trust (§2.5). Two consecutive delimiters are an
  ignored empty segment.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import Iterator

from ..ns2device.model import MAX_FRAME, PROTO_VERSION, FrameType

HEADER_SIZE = 7
COBS_DELIMITER = 0x00
CRC_INIT = 0xFFFF


class FramingError(ValueError):
    """A COBS block that does not decode."""


def crc16_ccitt_false(*segments: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly `0x1021`, init `0xffff`, no reflection).

    `binascii.crc_hqx` is the same polynomial with no reflection, so the
    container needs no table and the device's `esp_rom_crc16_be` agrees. Pass
    the header's first five bytes as one segment and the payload as the next —
    the two-segment form is part of the contract, not a convenience.
    """
    crc = CRC_INIT
    for segment in segments:
        crc = binascii.crc_hqx(segment, crc)
    return crc


def cobs_encode(data: bytes) -> bytes:
    """Standard COBS: the output contains no `0x00`, at one overhead byte per run."""
    out = bytearray([0])  # placeholder for the first block's code byte
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0x00:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 255:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(block: bytes) -> bytes:
    """Decode one `0x00`-free COBS block."""
    out = bytearray()
    index = 0
    length = len(block)
    while index < length:
        code = block[index]
        if code == 0:
            raise FramingError("COBS block contains a zero byte")
        index += 1
        end = index + code - 1
        if end > length:
            raise FramingError("COBS block runs past its end")
        out += block[index:end]
        if code < 255 and end < length:
            out.append(0x00)
        index = end
    return bytes(out)


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame. `type` and `verb` stay raw so an unknown value is
    still a frame the receiver can answer with a typed `ERROR` (§2.8)."""

    ver: int
    type: int
    verb: int
    payload: bytes = b""

    @property
    def frame_type_name(self) -> str:
        try:
            return FrameType(self.type).name
        except ValueError:
            return f"UNKNOWN({self.type})"

    def encode(self) -> bytes:
        """The full wire form, delimiters included."""
        head = bytes([self.ver, self.type, self.verb]) + struct.pack("<H", len(self.payload))
        crc = crc16_ccitt_false(head, self.payload)
        decoded = head + struct.pack("<H", crc) + self.payload
        return bytes([COBS_DELIMITER]) + cobs_encode(decoded) + bytes([COBS_DELIMITER])


def encode_request(verb: int, payload: bytes = b"", *, ver: int = PROTO_VERSION) -> bytes:
    return Frame(ver, int(FrameType.REQUEST), verb, payload).encode()


def decode_frame(decoded: bytes) -> Frame | None:
    """Decode a COBS-decoded block, or `None` if it must be discarded."""
    if len(decoded) < HEADER_SIZE:
        return None
    ver, frame_type, verb = decoded[0], decoded[1], decoded[2]
    length = struct.unpack_from("<H", decoded, 3)[0]
    crc = struct.unpack_from("<H", decoded, 5)[0]
    if len(decoded) != HEADER_SIZE + length:
        return None
    if crc != crc16_ccitt_false(decoded[:5], decoded[HEADER_SIZE:]):
        return None
    return Frame(ver, frame_type, verb, decoded[HEADER_SIZE:])


class FrameDecoder:
    """A streaming decoder: feed bytes, get the frames that survived the CRC.

    Resynchronisation never resets the link — a log line is a chunk that fails
    its CRC and is discarded, and the scan resumes at the next `0x00` (§2.2).
    Every dropped block is counted so the frame trace can show it.
    """

    def __init__(self, *, max_frame: int = MAX_FRAME) -> None:
        self._buffer = bytearray()
        self.max_frame = max_frame
        self.dropped = 0
        self.resyncs = 0

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buffer += data
        while True:
            index = self._buffer.find(COBS_DELIMITER)
            if index < 0:
                return
            segment = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            if not segment:
                continue  # consecutive delimiters are an empty segment (§2.2)
            try:
                decoded = cobs_decode(segment)
            except FramingError:
                self._discard()
                continue
            if len(decoded) > self.max_frame:
                self._discard()
                continue
            frame = decode_frame(decoded)
            if frame is None:
                self._discard()
                continue
            yield frame

    def _discard(self) -> None:
        self.dropped += 1
        self.resyncs += 1


__all__ = [
    "COBS_DELIMITER",
    "CRC_INIT",
    "FramingError",
    "Frame",
    "FrameDecoder",
    "HEADER_SIZE",
    "cobs_decode",
    "cobs_encode",
    "crc16_ccitt_false",
    "decode_frame",
    "encode_request",
]
