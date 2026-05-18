"""
Shared data models for the event-consumer package.

Defines the result enumerations and lightweight data classes used across
the various sub-modules.  Keeping them here breaks circular-import chains
and makes the shared vocabulary explicit.
"""

from __future__ import annotations

from enum import Enum


class ProcessingResult(Enum):
    """Processing result for each message.

    Matches the original ProcessingResult enum in consumer.py exactly.
    String values are used in metrics labels and span attributes.
    """

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    TRANSIENT_FAILURE = "transient_failure"  # Temporary failure (will retry)
    PERMANENT_FAILURE = "permanent_failure"  # Permanent failure (send to DLQ)
