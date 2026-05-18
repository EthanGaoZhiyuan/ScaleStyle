"""Tests for trace_context.py — W3C trace context extraction from Kafka headers."""

import os
import sys
from pathlib import Path

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config
from trace_context import (
    extract_header_value,
    extract_trace_context,
    build_trace_carrier,
    forward_trace_headers,
)

# ── helpers ───────────────────────────────────────────────────────────────────

VALID_TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
VALID_TRACESTATE = "vendor=value"
EXPECTED_TRACE_ID = "0123456789abcdef0123456789abcdef"


def _headers(*pairs):
    """Build a list of (key, bytes) header tuples."""
    return [(k, v.encode("utf-8") if isinstance(v, str) else v) for k, v in pairs]


# ── extract_header_value ──────────────────────────────────────────────────────


class TestExtractHeaderValue:
    def test_returns_value_for_present_header(self):
        headers = _headers(("traceparent", VALID_TRACEPARENT))
        result = extract_header_value(headers, "traceparent")
        assert result == VALID_TRACEPARENT

    def test_returns_none_for_absent_header(self):
        headers = _headers(("other-header", "value"))
        result = extract_header_value(headers, "traceparent")
        assert result is None

    def test_returns_none_for_empty_headers(self):
        assert extract_header_value([], "traceparent") is None

    def test_returns_none_for_none_headers(self):
        assert extract_header_value(None, "traceparent") is None

    def test_returns_none_when_value_is_none(self):
        headers = [("traceparent", None)]
        result = extract_header_value(headers, "traceparent")
        assert result is None

    def test_returns_none_on_non_utf8_bytes(self):
        headers = [("traceparent", b"\xff\xfe")]
        result = extract_header_value(headers, "traceparent")
        assert result is None

    def test_first_matching_header_wins(self):
        headers = _headers(("traceparent", "first"), ("traceparent", "second"))
        result = extract_header_value(headers, "traceparent")
        assert result == "first"


# ── extract_trace_context ─────────────────────────────────────────────────────


class TestExtractTraceContext:
    def test_valid_traceparent_extracts_trace_id(self):
        headers = _headers((config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT))
        ctx = extract_trace_context(headers)
        assert ctx["traceparent"] == VALID_TRACEPARENT
        assert ctx["trace_id"] == EXPECTED_TRACE_ID

    def test_tracestate_extracted_when_present(self):
        headers = _headers(
            (config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT),
            (config.KAFKA_TRACESTATE_HEADER, VALID_TRACESTATE),
        )
        ctx = extract_trace_context(headers)
        assert ctx["tracestate"] == VALID_TRACESTATE

    def test_missing_traceparent_returns_none_trace_id(self):
        ctx = extract_trace_context([])
        assert ctx["traceparent"] is None
        assert ctx["trace_id"] is None

    def test_missing_header_no_error_raised(self):
        """Absence of trace headers must never raise an exception."""
        ctx = extract_trace_context(None)
        assert ctx["traceparent"] is None
        assert ctx["tracestate"] is None
        assert ctx["trace_id"] is None

    def test_malformed_traceparent_does_not_extract_trace_id(self):
        """A traceparent that can't be split into 4 parts yields trace_id=None."""
        headers = _headers((config.KAFKA_TRACEPARENT_HEADER, "not-valid"))
        ctx = extract_trace_context(headers)
        # "not-valid".split("-") has 2 parts, fewer than 4 → trace_id stays None
        assert ctx["trace_id"] is None
        assert ctx["traceparent"] == "not-valid"

    def test_returns_dict_with_all_three_keys(self):
        ctx = extract_trace_context([])
        assert set(ctx.keys()) == {"traceparent", "tracestate", "trace_id"}


# ── build_trace_carrier ───────────────────────────────────────────────────────


class TestBuildTraceCarrier:
    def test_carrier_contains_traceparent_when_present(self):
        headers = _headers((config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT))
        carrier = build_trace_carrier(headers)
        assert carrier[config.KAFKA_TRACEPARENT_HEADER] == VALID_TRACEPARENT

    def test_carrier_contains_tracestate_when_present(self):
        headers = _headers(
            (config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT),
            (config.KAFKA_TRACESTATE_HEADER, VALID_TRACESTATE),
        )
        carrier = build_trace_carrier(headers)
        assert carrier[config.KAFKA_TRACESTATE_HEADER] == VALID_TRACESTATE

    def test_carrier_is_empty_when_no_trace_headers(self):
        carrier = build_trace_carrier([])
        assert carrier == {}

    def test_carrier_omits_tracestate_when_absent(self):
        headers = _headers((config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT))
        carrier = build_trace_carrier(headers)
        assert config.KAFKA_TRACESTATE_HEADER not in carrier


# ── forward_trace_headers ─────────────────────────────────────────────────────


class TestForwardTraceHeaders:
    def test_re_encodes_traceparent_as_bytes(self):
        headers = _headers((config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT))
        forwarded = forward_trace_headers(headers)
        assert (
            config.KAFKA_TRACEPARENT_HEADER,
            VALID_TRACEPARENT.encode("utf-8"),
        ) in forwarded

    def test_re_encodes_tracestate_as_bytes(self):
        headers = _headers(
            (config.KAFKA_TRACEPARENT_HEADER, VALID_TRACEPARENT),
            (config.KAFKA_TRACESTATE_HEADER, VALID_TRACESTATE),
        )
        forwarded = forward_trace_headers(headers)
        assert (
            config.KAFKA_TRACESTATE_HEADER,
            VALID_TRACESTATE.encode("utf-8"),
        ) in forwarded

    def test_returns_empty_list_when_no_trace_headers(self):
        assert forward_trace_headers([]) == []

    def test_returns_empty_list_for_none_headers(self):
        assert forward_trace_headers(None) == []
