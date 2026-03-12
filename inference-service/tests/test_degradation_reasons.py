"""
Tests for canonical degradation reason vocabulary.

Contract: docs/DEGRADATION_REASONS.md.
"""

from src.degradation import DegradationReason

CANONICAL_REASONS = frozenset(
    {
        "REDIS_TIMEOUT",
        "REDIS_UNAVAILABLE",
        "PERSONALIZATION_UNAVAILABLE",
        "INFERENCE_TIMEOUT",
        "INFERENCE_UNAVAILABLE",
        "CACHE_MISS",
        "STALE_DATA_ALLOWED",
        "DOWNSTREAM_CIRCUIT_OPEN",
        "DOWNSTREAM_CAPACITY_REJECTED",
        "EMPTY_RESULTS_ALLOWED",
    }
)


def test_degradation_reason_values_are_canonical():
    """All enum values must match the canonical vocabulary."""
    for reason in DegradationReason:
        assert (
            reason.value in CANONICAL_REASONS
        ), f"{reason.name} value {reason.value} not in canonical set"
        assert (
            reason.value == reason.name
        ), f"{reason.name} value should match name for .name() compatibility"


def test_degradation_reason_no_legacy_strings():
    """No ad-hoc or legacy string variants."""
    for reason in DegradationReason:
        assert reason.value.isupper(), f"{reason.name} must be UPPER_SNAKE_CASE"
        assert "_" in reason.value or len(reason.value) > 2
        assert " " not in reason.value
        assert reason.value == reason.value.replace(
            "-", "_"
        ), "Use underscore not hyphen"


def test_degradation_reason_used_in_metrics_and_logs():
    """Reason values are suitable for Prometheus labels and structured logs."""
    for reason in DegradationReason:
        # Prometheus labels: low cardinality, no special chars
        assert len(reason.value) <= 50
        assert all(c.isalnum() or c == "_" for c in reason.value)


def test_canonical_set_is_complete():
    """Canonical set includes all expected reasons from contract."""
    expected = {
        "REDIS_TIMEOUT",
        "REDIS_UNAVAILABLE",
        "PERSONALIZATION_UNAVAILABLE",
        "INFERENCE_TIMEOUT",
        "INFERENCE_UNAVAILABLE",
        "CACHE_MISS",
        "STALE_DATA_ALLOWED",
        "DOWNSTREAM_CIRCUIT_OPEN",
        "DOWNSTREAM_CAPACITY_REJECTED",
        "EMPTY_RESULTS_ALLOWED",
    }
    assert CANONICAL_REASONS == expected
    assert len(DegradationReason) == len(expected)
