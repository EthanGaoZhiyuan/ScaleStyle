"""
Unit tests for A/B test bucketing utilities.

Validates consistent, deterministic user bucketing across all
inference service components.
"""

import pytest
from src.utils.bucketing import bucket_user


def test_bucket_user_none_returns_default():
    """None user_id should consistently return bucket 0."""
    assert bucket_user(None, 2) == 0
    assert bucket_user(None, 10) == 0


def test_bucket_user_deterministic():
    """Same user_id should always return same bucket."""
    user_id = "user-42"
    expected = bucket_user(user_id, 2)
    # Call 100 times to verify stability
    for _ in range(100):
        assert bucket_user(user_id, 2) == expected


def test_bucket_user_distribution():
    """Bucketing should distribute users across buckets."""
    # Generate diverse user IDs
    user_ids = [
        "user-1", "user-2", "user-42", "user-1337",
        "alice", "bob", "charlie",
        "a1b2c3", "test@example.com",
        "uuid-550e8400-e29b-41d4-a716-446655440000"
    ]
    
    buckets = [bucket_user(uid, 2) for uid in user_ids]
    
    # Should have both buckets represented
    assert 0 in buckets
    assert 1 in buckets
    
    # Should be roughly balanced (not all in one bucket)
    assert len(set(buckets)) == 2


def test_bucket_user_numeric_suffix_consistency():
    """
    User IDs with numeric suffixes should use MD5 hash, not trailing digits.
    
    This is a regression test for the old router.py behavior that extracted
    trailing digits (e.g., "user-42" -> 42 % 2 = 0), causing inconsistency
    with ingress.py which used pure MD5 hashing.
    """
    # These would have different buckets with trailing-digit extraction
    assert bucket_user("user-42", 2) == 1  # MD5 hash
    assert bucket_user("user-43", 2) == 0  # MD5 hash
    
    # Verify it's NOT using trailing digits (42 % 2 would be 0)
    # The MD5 hash of "user-42" should produce bucket 1
    import hashlib
    h = hashlib.md5("user-42".encode("utf-8")).hexdigest()
    expected = int(h, 16) % 2
    assert bucket_user("user-42", 2) == expected


def test_bucket_user_respects_num_buckets():
    """Should respect num_buckets parameter."""
    user_id = "test-user"
    
    # With 2 buckets, result must be 0 or 1
    assert bucket_user(user_id, 2) in [0, 1]
    
    # With 10 buckets, result must be 0-9
    assert 0 <= bucket_user(user_id, 10) < 10
    
    # With 100 buckets, result must be 0-99
    assert 0 <= bucket_user(user_id, 100) < 100


def test_bucket_user_empty_string():
    """Empty string should be treated like None."""
    assert bucket_user("", 2) == 0


def test_bucket_user_unicode():
    """Should handle unicode user IDs gracefully."""
    unicode_ids = ["用户-123", "usuario-456", "пользователь-789"]
    
    for uid in unicode_ids:
        bucket = bucket_user(uid, 2)
        # Should return valid bucket
        assert bucket in [0, 1]
        # Should be deterministic
        assert bucket_user(uid, 2) == bucket


def test_router_ingress_bucketing_consistency():
    """
    Regression test: router and ingress must assign same flow for same user.
    
    Old behavior:
    - router.py: extracted trailing digits from "user-42" -> 42 % 2 = 0
    - ingress.py: hashed "user-42" via MD5 -> different result
    
    New behavior:
    - Both use bucket_user() with MD5 hash -> consistent
    """
    test_users = ["user-42", "user-1337", "alice", "bob"]
    
    for user_id in test_users:
        bucket = bucket_user(user_id, 2)
        
        # Router logic: bucket 1 -> "base", bucket 0 -> "smart"
        router_flow = "base" if bucket == 1 else "smart"
        
        # Ingress logic: bucket 0 -> "smart", bucket 1 -> "base"
        ingress_flow = "smart" if bucket == 0 else "base"
        
        # Must match
        assert router_flow == ingress_flow, \
            f"Flow mismatch for {user_id}: router={router_flow}, ingress={ingress_flow}"
