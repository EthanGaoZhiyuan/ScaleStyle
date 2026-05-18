"""
Kafka version-compat helpers.

kafka-python 2.0.2: OffsetAndMetadata(offset, metadata)            — 2 positional args
kafka-python 2.3.x: OffsetAndMetadata(offset, metadata, leader_epoch) — 3 positional args

The signature is detected once at import time so every call site stays clean.
"""

import inspect

from kafka import OffsetAndMetadata

# leader_epoch was added in kafka-python 2.3.x. Detect by counting __new__ params:
# 2.0.2 → (_cls, offset, metadata)        = 3 params
# 2.3.x → (_cls, offset, metadata, leader_epoch) = 4 params
_OAM_NEEDS_LEADER_EPOCH: bool = (
    len(inspect.signature(OffsetAndMetadata.__new__).parameters) >= 4
)


def make_offset_and_metadata(offset: int) -> OffsetAndMetadata:
    """Build an OffsetAndMetadata for the given offset, compatible with both 2.0.x and 2.3.x."""
    if _OAM_NEEDS_LEADER_EPOCH:
        return OffsetAndMetadata(offset, None, -1)  # leader_epoch=-1 means unknown
    return OffsetAndMetadata(offset, None)
