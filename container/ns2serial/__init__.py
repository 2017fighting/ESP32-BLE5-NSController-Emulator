"""The serial seam (§8.2): COBS + CRC framing, log demux, resync on CRC failure.

The pure codec is here and tested; the asyncio port and the one-outstanding
request loop are issue #30's, behind `SerialTransport`.
"""

from __future__ import annotations

from .api import (
    DEFAULT_BAUD,
    DEFAULT_PORT,
    FALLBACK_BAUD,
    SerialTransport,
    TransportUnavailable,
)
from .framing import (
    COBS_DELIMITER,
    HEADER_SIZE,
    Frame,
    FrameDecoder,
    FramingError,
    cobs_decode,
    cobs_encode,
    crc16_ccitt_false,
    decode_frame,
    encode_request,
)

__all__ = [
    "COBS_DELIMITER",
    "DEFAULT_BAUD",
    "DEFAULT_PORT",
    "FALLBACK_BAUD",
    "Frame",
    "FrameDecoder",
    "FramingError",
    "HEADER_SIZE",
    "SerialTransport",
    "TransportUnavailable",
    "cobs_decode",
    "cobs_encode",
    "crc16_ccitt_false",
    "decode_frame",
    "encode_request",
]
