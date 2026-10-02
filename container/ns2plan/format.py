"""The plan binary format (§5.3), as constants shared by the compiler and cache.

Little-endian throughout. A plan is a 12-byte header plus one 11-byte record
per distinct controller state; ``payload_size`` is the only place the two
sizes combine.
"""

from __future__ import annotations

PLAN_MAGIC = 0x4C50534E
PLAN_MAGIC_BYTES = b"NSPL"  # 0x4C50534E little-endian
PLAN_FORMAT_VERSION = 1
PLAN_RECORD_SIZE = 11
PLAN_HEADER_SIZE = 12

IDENTITY_LENGTH = 16  # §5.6 — SHA-256 truncated to 16 bytes

# §5.3 header field offsets.
HEADER_OFFSET_MAGIC = 0
HEADER_OFFSET_FORMAT_VERSION = 4
HEADER_OFFSET_RECORD_SIZE = 5
HEADER_OFFSET_RECORD_COUNT = 6
HEADER_OFFSET_LOOP_MS = 8

# §5.3 record field offsets.
RECORD_OFFSET_BUTTONS = 0
RECORD_OFFSET_LEFT = 3
RECORD_OFFSET_RIGHT = 6
RECORD_OFFSET_HOLD_MS = 9

# §5.5 — a single hold is a u16.
MAX_HOLD_MS = 0xFFFF


def payload_size(record_count: int) -> int:
    """Payload length in bytes for a plan holding ``record_count`` records."""
    return PLAN_HEADER_SIZE + PLAN_RECORD_SIZE * record_count
