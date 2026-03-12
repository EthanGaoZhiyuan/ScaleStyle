"""Minimal OpenTelemetry setup for the event consumer."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_tracer_initialized = False
_missing_dependency_logged = False


class _NoopSpan:
    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    def set_attribute(self, name: str, value: Any) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, *args: Any, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


try:
    from opentelemetry import trace
    from opentelemetry.context import get_current
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import SpanKind

    OTEL_AVAILABLE = True
    SPAN_KIND_CONSUMER = SpanKind.CONSUMER
except ImportError:
    trace = None
    OTEL_AVAILABLE = False
    SPAN_KIND_CONSUMER = None


def _current_context() -> Any:
    if not OTEL_AVAILABLE:
        return None
    return get_current()


def extract_context(carrier: dict[str, str]) -> Any:
    """Extract parent tracing context from Kafka headers."""
    if not OTEL_AVAILABLE or not carrier:
        return _current_context()

    try:
        return extract(carrier=carrier)
    except Exception as exc:
        logger.debug("Failed to extract trace context from Kafka headers: %s", exc)
        return _current_context()


def setup_tracing(service_name: str = "event-consumer") -> Any:
    """Initialize OpenTelemetry tracing once per process."""
    global _tracer_initialized, _missing_dependency_logged

    if not OTEL_AVAILABLE:
        if not _missing_dependency_logged:
            logger.info(
                "OpenTelemetry packages not installed; distributed tracing disabled for %s",
                service_name,
            )
            _missing_dependency_logged = True
        return _NoopTracer()

    if _tracer_initialized:
        return trace.get_tracer(service_name)

    current_provider = trace.get_tracer_provider()
    if current_provider.__class__.__name__ != "ProxyTracerProvider":
        _tracer_initialized = True
        return trace.get_tracer(service_name)

    traces_exporter = os.getenv("OTEL_TRACES_EXPORTER", "otlp").lower()
    if traces_exporter == "none":
        _tracer_initialized = True
        logger.info("OpenTelemetry traces exporter disabled for %s", service_name)
        return trace.get_tracer(service_name)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger-collector:4317")
    insecure = otlp_endpoint.startswith("http://")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.instance.id": os.getenv("HOSTNAME", "local-instance"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=otlp_endpoint,
                insecure=insecure,
            )
        )
    )
    trace.set_tracer_provider(provider)
    _tracer_initialized = True
    logger.info("OpenTelemetry tracing initialized for %s -> %s", service_name, otlp_endpoint)
    return trace.get_tracer(service_name)