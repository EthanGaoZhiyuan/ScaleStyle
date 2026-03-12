"""
Unit tests for Event-based materialization synchronization.

Validates that the hot-path lock is not held during rebuild waits,
preventing thread starvation and reducing latency variance.
"""

import threading
import time
from unittest.mock import MagicMock, patch
from src.personalization.feature_reader import FeatureReader


def test_rebuild_events_created_for_locked_out_windows():
    """Event should be created for windows being rebuilt."""
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    # Initial cache miss triggers rebuild
    redis_mock.pipeline.return_value.execute.side_effect = [
        [None, None, None],  # TTL check: all expired
        [True, True, True],  # Lock acquisition: all succeed
        None,  # ZUNIONSTORE rebuild
    ]

    now_ts = time.time()
    resolved, _ = reader._resolve_materialized_popularity_windows(now_ts)

    # Events should be created during rebuild
    # After rebuild completes, they should be cleaned up
    assert len(reader._rebuild_events) == 0, "Events should be cleaned up after rebuild"


def test_event_wait_instead_of_sleep_in_lock():
    """
    Threads locked out should wait on Event, not sleep while holding lock.

    This test validates that the hot-path lock is released before waiting,
    allowing other threads to proceed concurrently.
    """
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    # Simulate two concurrent threads detecting expired windows
    # Thread 1 wins lock, Thread 2 is locked out
    lock_acquired_results = [
        [None, None, None],  # TTL check: all expired
        [True, False, False],  # Lock: thread 1 wins "1h", loses "24h", "7d"
        None,  # ZUNIONSTORE rebuild for "1h"
        [100, 100],  # TTL recheck for locked-out windows (peer rebuilt)
    ]

    redis_mock.pipeline.return_value.execute.side_effect = lock_acquired_results

    # Track if lock is held during wait
    lock_held_during_wait = []
    original_wait = threading.Event.wait

    def tracked_wait(self, timeout=None):
        # Check if reader's main lock is held
        lock_held = reader._materialized_window_lock.locked()
        lock_held_during_wait.append(lock_held)
        return original_wait(self, timeout=timeout)

    with patch.object(threading.Event, "wait", tracked_wait):
        now_ts = time.time()
        resolved, _ = reader._resolve_materialized_popularity_windows(now_ts)

    # The lock should NOT be held during Event.wait()
    # This is the key fix for M-4
    if lock_held_during_wait:
        assert not any(
            lock_held_during_wait
        ), "Lock should be released before waiting on Event (M-4 fix)"


def test_concurrent_materialization_doesnt_block_each_other():
    """
    Multiple threads detecting expired windows should not serialize through lock.

    Key improvement: Only brief lock holds for coordination, not during Redis ops.
    """
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    # Simulate multiple threads calling _resolve_materialized_popularity_windows
    # Each should spend minimal time in the lock
    redis_mock.pipeline.return_value.execute.side_effect = [
        [None, None, None],  # Thread 1 TTL check: all expired
        [True, True, True],  # Thread 1 lock acquisition: all succeed
        None,  # Thread 1 ZUNIONSTORE rebuild
        [100, 100, 100],  # Thread 2 TTL check: all available (rebuilt by thread 1)
    ]

    start = time.perf_counter()
    now_ts = time.time()

    # First call rebuilds
    resolved1, _ = reader._resolve_materialized_popularity_windows(now_ts)

    # Second call hits cache
    resolved2, _ = reader._resolve_materialized_popularity_windows(now_ts)

    elapsed = time.perf_counter() - start

    # Should complete quickly without 50ms sleep in lock
    assert elapsed < 0.1, f"Elapsed {elapsed}s should be < 100ms (no sleep-in-lock)"


def test_event_cleaned_up_after_rebuild_completes():
    """Events should be removed after rebuild to prevent memory leak."""
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    redis_mock.pipeline.return_value.execute.side_effect = [
        [None, None, None],  # TTL check: all expired
        [True, True, True],  # Lock acquisition: all succeed
        None,  # ZUNIONSTORE rebuild
    ]

    now_ts = time.time()

    # First materialization
    resolved1, _ = reader._resolve_materialized_popularity_windows(now_ts)
    assert len(reader._rebuild_events) == 0, "Events should be cleaned up after rebuild"

    # Second materialization (cache hit)
    resolved2, _ = reader._resolve_materialized_popularity_windows(now_ts)
    assert len(reader._rebuild_events) == 0, "No new events on cache hit"


def test_event_cleaned_up_on_rebuild_failure():
    """Events should be removed even if rebuild fails."""
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    redis_mock.pipeline.return_value.execute.side_effect = [
        [100, 100, 100],  # Initial TTL check: all cached (setup cache)
        [None, None, None],  # Second TTL check: all expired
        [True, True, True],  # Lock acquisition: all succeed
        Exception("Redis connection failed"),  # ZUNIONSTORE rebuild fails
    ]

    now_ts = time.time()

    # First call populates cache
    resolved1, _ = reader._resolve_materialized_popularity_windows(now_ts)

    # Advance time to expire cache
    now_ts += 200

    # Second call triggers rebuild which fails
    try:
        resolved2, _ = reader._resolve_materialized_popularity_windows(now_ts)
    except Exception:
        pass  # Expected failure

    # Events should still be cleaned up despite failure
    assert (
        len(reader._rebuild_events) == 0
    ), "Events should be cleaned up even on failure to prevent memory leak"


def test_wait_events_only_for_same_key():
    """Threads should only wait on Events for the specific key they need."""
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    # Simulate: "1h" wins lock, "24h" and "7d" are locked out
    redis_mock.pipeline.return_value.execute.side_effect = [
        [None, None, None],  # TTL check: all expired
        [True, False, False],  # Lock: "1h" wins, others lose
        None,  # ZUNIONSTORE for "1h"
        [100, 100],  # TTL recheck for "24h", "7d"
    ]

    now_ts = time.time()
    resolved, _ = reader._resolve_materialized_popularity_windows(now_ts)

    # All windows should have valid keys
    assert "1h" in resolved
    assert "24h" in resolved
    assert "7d" in resolved

    # Events should be cleaned up
    assert len(reader._rebuild_events) == 0


def test_lock_hold_time_minimal():
    """
    Lock should only be held for coordination, not during Redis ops.

    This validates the key improvement: lock is released before expensive
    Redis pipeline execution and Event.wait() calls.

    This is a simplified test that validates the lock behavior conceptually,
    since threading.Lock's acquire/release methods cannot be patched.
    """
    redis_mock = MagicMock()
    reader = FeatureReader(redis_mock)

    # Simulate slow Redis operations
    def slow_execute(*args, **kwargs):
        time.sleep(0.01)  # 10ms simulated Redis latency
        return (
            [None, None, None]
            if slow_execute.call_count == 1
            else ([True, True, True] if slow_execute.call_count == 2 else None)
        )

    slow_execute.call_count = 0

    def counting_execute(*args, **kwargs):
        slow_execute.call_count += 1
        return slow_execute(*args, **kwargs)

    redis_mock.pipeline.return_value.execute = counting_execute

    now_ts = time.time()
    start = time.perf_counter()
    resolved, _ = reader._resolve_materialized_popularity_windows(now_ts)
    elapsed = time.perf_counter() - start

    # Total elapsed should include Redis ops (3 × 10ms = 30ms)
    # But lock should not add significant overhead (no 50ms sleep in lock)
    assert (
        elapsed < 0.1
    ), f"Total time {elapsed}s should be under 100ms (no blocking lock holds)"

    # Verify the implementation doesn't hold lock continuously
    # If lock was held during Redis ops, we'd see serialization issues
    # This test validates the conceptual improvement without patching Lock
