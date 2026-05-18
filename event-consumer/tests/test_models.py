"""Tests for models.py — shared data models."""

import os
import sys
from pathlib import Path

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from models import ProcessingResult


class TestProcessingResult:
    def test_applied_value(self):
        assert ProcessingResult.APPLIED.value == "applied"

    def test_duplicate_value(self):
        assert ProcessingResult.DUPLICATE.value == "duplicate"

    def test_transient_failure_value(self):
        assert ProcessingResult.TRANSIENT_FAILURE.value == "transient_failure"

    def test_permanent_failure_value(self):
        assert ProcessingResult.PERMANENT_FAILURE.value == "permanent_failure"

    def test_fields_accessible(self):
        result = ProcessingResult.APPLIED
        assert result.value == "applied"

    def test_enum_members_count(self):
        """Exactly four outcomes defined — no silent additions."""
        assert len(ProcessingResult) == 4

    def test_enum_identity(self):
        assert ProcessingResult("applied") is ProcessingResult.APPLIED
        assert ProcessingResult("duplicate") is ProcessingResult.DUPLICATE
        assert (
            ProcessingResult("transient_failure") is ProcessingResult.TRANSIENT_FAILURE
        )
        assert (
            ProcessingResult("permanent_failure") is ProcessingResult.PERMANENT_FAILURE
        )

    def test_consumer_module_reexports_same_class(self):
        """consumer.ProcessingResult must be the same object as models.ProcessingResult."""
        from consumer import ProcessingResult as ConsumerProcessingResult

        assert ConsumerProcessingResult is ProcessingResult
