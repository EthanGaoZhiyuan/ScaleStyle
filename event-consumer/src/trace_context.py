"""
W3C Trace Context extraction from Kafka message headers.

Reads the ``traceparent`` and ``tracestate`` headers that the gateway stamps
on every Kafka record and exposes helper functions used by the consumer loop
to propagate distributed traces across the retry / DLQ hop chain.

The W3C traceparent format is::

    00-<trace-id>-<span-id>-<flags>

where ``<trace-id>`` is the 32-hex-character distributed trace identifier
extracted for structured logging and DLQ payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

import config

if TYPE_CHECKING:
    pass


def extract_header_value(
    headers: Union[List[Tuple[str, bytes]], None], header_key: str
) -> Optional[str]:
    """Decode a single Kafka header value as a UTF-8 string.

    Parameters
    ----------
    headers:
        The ``message.headers`` list of ``(key, value)`` byte tuples, or
        ``None`` / empty list when no headers are present.
    header_key:
        The header name to look up (case-sensitive, as per Kafka convention).

    Returns
    -------
    str or None
        The decoded header value, or ``None`` if the header is absent or
        cannot be decoded.
    """
    if not headers:
        return None
    for key, value in headers:
        if key == header_key and value is not None:
            try:
                return value.decode("utf-8")
            except Exception:
                return None
    return None


def extract_trace_context(
    headers: Union[List[Tuple[str, bytes]], None],
) -> Dict[str, Optional[str]]:
    """Read W3C trace context headers from a Kafka message header list.

    Returns a dict with keys ``traceparent``, ``tracestate``, and
    ``trace_id``.  All values may be ``None`` when the producing service did
    not attach trace headers.

    Parameters
    ----------
    headers:
        The ``message.headers`` list of ``(key, bytes)`` tuples.

    Returns
    -------
    dict
        ``{"traceparent": str|None, "tracestate": str|None, "trace_id": str|None}``
    """
    traceparent = extract_header_value(headers, config.KAFKA_TRACEPARENT_HEADER)
    tracestate = extract_header_value(headers, config.KAFKA_TRACESTATE_HEADER)

    trace_id: Optional[str] = None
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 4:
            trace_id = parts[1]

    return {
        "traceparent": traceparent,
        "tracestate": tracestate,
        "trace_id": trace_id,
    }


def build_trace_carrier(
    headers: Union[List[Tuple[str, bytes]], None],
) -> Dict[str, str]:
    """Convert Kafka trace headers into an OpenTelemetry propagator carrier.

    The returned dict is a W3C carrier suitable for passing to
    ``opentelemetry.propagate.extract(carrier=...)``.  Keys are omitted
    when the corresponding header value is absent.

    Parameters
    ----------
    headers:
        The ``message.headers`` list.

    Returns
    -------
    dict
        A mapping of header name → decoded string value.
    """
    trace_ctx = extract_trace_context(headers)
    carrier: Dict[str, str] = {}
    if trace_ctx["traceparent"]:
        carrier[config.KAFKA_TRACEPARENT_HEADER] = trace_ctx["traceparent"]
    if trace_ctx["tracestate"]:
        carrier[config.KAFKA_TRACESTATE_HEADER] = trace_ctx["tracestate"]
    return carrier


def forward_trace_headers(
    headers: Union[List[Tuple[str, bytes]], None],
) -> List[Tuple[str, bytes]]:
    """Build Kafka headers to preserve trace context on retry / DLQ hops.

    Re-encodes the decoded trace values back to bytes so they can be
    attached to a ``KafkaProducer.send()`` call.

    Parameters
    ----------
    headers:
        The ``message.headers`` list from the source message.

    Returns
    -------
    list of (str, bytes)
        Zero, one, or two header tuples depending on which trace headers
        were present in the source message.
    """
    result: List[Tuple[str, bytes]] = []
    trace_ctx = extract_trace_context(headers)
    if trace_ctx["traceparent"]:
        result.append(
            (
                config.KAFKA_TRACEPARENT_HEADER,
                trace_ctx["traceparent"].encode("utf-8"),
            )
        )
    if trace_ctx["tracestate"]:
        result.append(
            (
                config.KAFKA_TRACESTATE_HEADER,
                trace_ctx["tracestate"].encode("utf-8"),
            )
        )
    return result
