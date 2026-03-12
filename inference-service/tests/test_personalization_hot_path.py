"""
Tests enforcing snapshot-only hot path. Legacy fine-grained readers must not
be used by serving code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personalization.feature_reader import FeatureReader
from personalization.null_feature_reader import NullFeatureReader


def test_feature_reader_implements_snapshot_loader_only():
    """FeatureReader used by serving code must implement only PersonalizationSnapshotLoader."""
    assert hasattr(FeatureReader, "load_personalization_snapshot")
    # Must NOT have legacy methods that could cause Redis fan-out
    assert not hasattr(FeatureReader, "get_user_recent_clicks")
    assert not hasattr(FeatureReader, "get_user_category_affinity")
    assert not hasattr(FeatureReader, "get_item_categories_batch")
    assert not hasattr(FeatureReader, "get_session_clicks")
    assert not hasattr(FeatureReader, "get_item_popularity_signals")


def test_null_feature_reader_implements_snapshot_loader_only():
    """NullFeatureReader must expose only load_personalization_snapshot."""
    assert hasattr(NullFeatureReader, "load_personalization_snapshot")
    assert not hasattr(NullFeatureReader, "get_user_recent_clicks")
    assert not hasattr(NullFeatureReader, "get_user_category_affinity")
    assert not hasattr(NullFeatureReader, "get_item_categories_batch")
    assert not hasattr(NullFeatureReader, "get_item_popularity_signals")


def test_feature_reader_exposes_only_load_snapshot_for_hot_path():
    """FeatureReader exposes load_personalization_snapshot as the sole hot-path entry."""
    assert hasattr(FeatureReader, "load_personalization_snapshot")
    assert callable(getattr(FeatureReader, "load_personalization_snapshot"))


def test_ingress_does_not_import_legacy_reader():
    """Serving code must not import LegacyFeatureReader."""
    ingress_path = Path(__file__).parent.parent / "src" / "deployments" / "ingress.py"
    source = ingress_path.read_text()
    assert "LegacyFeatureReader" not in source
    assert "feature_reader_legacy" not in source
