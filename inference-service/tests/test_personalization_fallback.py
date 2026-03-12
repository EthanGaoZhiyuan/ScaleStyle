"""
Tests for personalization fallback paths and degradation reason propagation.
"""
import pytest

from src.degradation import DegradationReason
from src.personalization import NullFeatureReader


def test_null_feature_reader_returns_canonical_degradation_reason():
    """NullFeatureReader snapshot uses PERSONALIZATION_UNAVAILABLE (canonical)."""
    reader = NullFeatureReader()
    snapshot = reader.load_personalization_snapshot("user-1", ["item-1"])
    assert snapshot.degraded is True
    assert len(snapshot.degraded_reasons) == 1
    assert snapshot.degraded_reasons[0] == DegradationReason.PERSONALIZATION_UNAVAILABLE
    assert snapshot.degraded_reasons[0].value == "PERSONALIZATION_UNAVAILABLE"
