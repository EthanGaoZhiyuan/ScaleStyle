"""
A/B test bucketing utilities for consistent user assignment.

This module provides a stable, deterministic bucketing algorithm
for A/B test experiments across the inference service.
"""

import hashlib
from typing import Optional


def bucket_user(user_id: Optional[str], num_buckets: int = 2) -> int:
    """
    Assign user to stable bucket using MD5 hash.

    Uses MD5 hash of user_id for consistent bucket assignment across
    all service components (router, ingress, etc). This ensures that
    the same user always lands in the same bucket regardless of which
    code path executes.

    Args:
        user_id: User identifier, or None for default bucket 0.
        num_buckets: Number of buckets to distribute users into (default: 2).

    Returns:
        int: Bucket number in range [0, num_buckets-1].

    Example:
        >>> bucket_user("user-42", 2)  # Returns 0 or 1 (stable for this ID)
        0
        >>> bucket_user("user-1337", 2)
        1
    """
    if not user_id:
        return 0  # Default to bucket 0
    # Use MD5 hash for stable, deterministic bucketing
    h = hashlib.md5(user_id.encode("utf-8")).hexdigest()
    return int(h, 16) % num_buckets
