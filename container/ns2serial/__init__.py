"""The serial seam (§8.2): COBS + CRC framing, log demux, resync on CRC failure.

The pure codec is here and tested; the asyncio port and the one-outstanding
request loop are issue #30's, behind `SerialTransport`.
"""

from __future__ import annotations

from .api import (
    DEFAULT_BAUD,
    DEFAULT_PORT,
    SerialTransport,
    TransportUnavailable,
)
from .frame_io import ACK_WINDOW, FrameTransport
from .port import PortBusy, SerialPortTransport, find_holder
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
    "ACK_WINDOW",
    "COBS_DELIMITER",
    "DEFAULT_BAUD",
    "DEFAULT_PORT",
    "Frame",
    "FrameDecoder",
    "FrameTransport",
    "FramingError",
    "HEADER_SIZE",
    "PortBusy",
    "SerialPortTransport",
    "SerialTransport",
    "TransportUnavailable",
    "cobs_decode",
    "cobs_encode",
    "crc16_ccitt_false",
    "decode_frame",
    "encode_request",
    "find_holder",
]
