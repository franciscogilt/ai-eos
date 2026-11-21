"""Time-ordered UUIDv7 identifiers (RFC 9562) for primary keys."""

from __future__ import annotations

import os
import time
import uuid

_MASK_48 = (1 << 48) - 1
_MASK_62 = (1 << 62) - 1


def uuid7() -> uuid.UUID:
    """48-bit Unix milliseconds, version 7, 74 random bits.

    Ordering is by millisecond; values created in the same millisecond are not
    ordered among themselves, which is fine for B-tree locality.
    """
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand & 0xFFF
    rand_b = (rand >> 12) & _MASK_62
    value = ((ms & _MASK_48) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
