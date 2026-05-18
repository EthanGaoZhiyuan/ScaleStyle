"""Tests for kafka_utils.make_offset_and_metadata version compatibility.

Strategy: test against two stub OffsetAndMetadata classes that simulate
kafka-python 2.0.2 (2-arg) and 2.3.x (3-arg) respectively. Each test
uses a self-contained module reload that restores all affected sys.modules
entries on exit, so this file never pollutes the shared test session.
"""
import sys
import types
import inspect
import contextlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Stub OffsetAndMetadata classes
# ---------------------------------------------------------------------------


class _OAM_TwoArg:
    """Simulates kafka-python 2.0.2: OffsetAndMetadata(offset, metadata)."""

    def __new__(cls, offset, metadata):
        obj = object.__new__(cls)
        obj.offset = offset
        obj.metadata = metadata
        return obj


class _OAM_ThreeArg:
    """Simulates kafka-python 2.3.x: OffsetAndMetadata(offset, metadata, leader_epoch)."""

    def __new__(cls, offset, metadata, leader_epoch):
        obj = object.__new__(cls)
        obj.offset = offset
        obj.metadata = metadata
        obj.leader_epoch = leader_epoch
        return obj


# ---------------------------------------------------------------------------
# Context manager: reload kafka_utils with a specific stub, full cleanup
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _kafka_utils_with_stub(oam_cls):
    """
    Temporarily replace sys.modules["kafka"] and force a fresh reload of
    kafka_utils.  All affected entries are restored on exit regardless of
    exceptions.
    """
    saved_kafka = sys.modules.get("kafka")
    saved_utils = sys.modules.get("kafka_utils")

    kafka_stub = types.ModuleType("kafka")
    kafka_stub.OffsetAndMetadata = oam_cls
    kafka_stub.TopicPartition = object  # unused by kafka_utils
    sys.modules["kafka"] = kafka_stub
    sys.modules.pop("kafka_utils", None)

    try:
        import kafka_utils as ku

        yield ku
    finally:
        # Always restore — even on AssertionError / unexpected exception
        if saved_kafka is not None:
            sys.modules["kafka"] = saved_kafka
        else:
            sys.modules.pop("kafka", None)

        if saved_utils is not None:
            sys.modules["kafka_utils"] = saved_utils
        else:
            sys.modules.pop("kafka_utils", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMakeOffsetAndMetadata:
    def test_two_arg_form_offset_is_correct(self):
        with _kafka_utils_with_stub(_OAM_TwoArg) as ku:
            assert ku._OAM_NEEDS_LEADER_EPOCH is False
            result = ku.make_offset_and_metadata(42)
            assert result.offset == 42
            assert result.metadata is None

    def test_three_arg_form_offset_is_correct(self):
        with _kafka_utils_with_stub(_OAM_ThreeArg) as ku:
            assert ku._OAM_NEEDS_LEADER_EPOCH is True
            result = ku.make_offset_and_metadata(42)
            assert result.offset == 42
            assert result.metadata is None
            assert result.leader_epoch == -1

    def test_two_arg_form_does_not_pass_leader_epoch(self):
        # _OAM_TwoArg raises TypeError when called with 3 positional args
        with pytest.raises(TypeError):
            _OAM_TwoArg(42, None, -1)
        # make_offset_and_metadata must route correctly (no TypeError raised)
        with _kafka_utils_with_stub(_OAM_TwoArg) as ku:
            ku.make_offset_and_metadata(42)  # must not raise

    def test_three_arg_form_requires_leader_epoch(self):
        # _OAM_ThreeArg raises TypeError when called with only 2 positional args
        with pytest.raises(TypeError):
            _OAM_ThreeArg(42, None)
        # make_offset_and_metadata must supply leader_epoch (no TypeError)
        with _kafka_utils_with_stub(_OAM_ThreeArg) as ku:
            ku.make_offset_and_metadata(42)  # must not raise

    def test_zero_offset_two_arg(self):
        with _kafka_utils_with_stub(_OAM_TwoArg) as ku:
            assert ku.make_offset_and_metadata(0).offset == 0

    def test_zero_offset_three_arg(self):
        with _kafka_utils_with_stub(_OAM_ThreeArg) as ku:
            assert ku.make_offset_and_metadata(0).offset == 0

    def test_detection_uses_new_param_count(self):
        """_OAM_NEEDS_LEADER_EPOCH is driven by __new__ parameter count."""
        two_arg_count = len(inspect.signature(_OAM_TwoArg.__new__).parameters)
        three_arg_count = len(inspect.signature(_OAM_ThreeArg.__new__).parameters)
        assert two_arg_count == 3   # cls, offset, metadata
        assert three_arg_count == 4  # cls, offset, metadata, leader_epoch

        with _kafka_utils_with_stub(_OAM_TwoArg) as ku:
            assert ku._OAM_NEEDS_LEADER_EPOCH is False
        with _kafka_utils_with_stub(_OAM_ThreeArg) as ku:
            assert ku._OAM_NEEDS_LEADER_EPOCH is True
