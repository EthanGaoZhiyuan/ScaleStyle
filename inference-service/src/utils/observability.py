"""
OpenTelemetry tracing setup for inference service.

This module initializes distributed tracing with Jaeger backend.
Traces are exported via OTLP to Jaeger collector.
"""

import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)

# Global flag to ensure TracerProvider is only initialized once per process
_tracer_initialized = False


def setup_tracing(service_name: str = "inference-service") -> trace.Tracer:
    """
    Initialize OpenTelemetry tracing with Jaeger backend (idempotent).

    This function is safe to call multiple times - it will only initialize
    the TracerProvider once per process, preventing issues with multiple
    Ray Serve replicas or deployment instances.

    Args:
        service_name: Name of this service for trace identification

    Returns:
        Configured tracer instance
    """
    global _tracer_initialized

    # Return existing tracer if already initialized (idempotent)
    if _tracer_initialized:
        logger.debug(
            f"Tracer already initialized, reusing existing provider for {service_name}"
        )
        return trace.get_tracer(__name__)

    # Get Jaeger endpoint from environment
    jaeger_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "jaeger:4317")
    if jaeger_endpoint.startswith("http://"):
        jaeger_endpoint = jaeger_endpoint[7:]  # Remove "http://" prefix
    elif jaeger_endpoint.startswith("https://"):
        jaeger_endpoint = jaeger_endpoint[8:]  # Remove "https://" prefix

    # Create resource with service name
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.instance.id": os.getenv("HOSTNAME", "local-instance"),
        }
    )

    # Initialize tracer provider
    provider = TracerProvider(resource=resource)

    # Configure OTLP exporter for Jaeger
    otlp_exporter = OTLPSpanExporter(
        endpoint=jaeger_endpoint,
        insecure=True,  # No TLS in local dev
    )

    # Add batch span processor
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    # Set as global tracer provider
    trace.set_tracer_provider(provider)

    # Mark as initialized to prevent re-initialization
    _tracer_initialized = True

    logger.info(
        f"✅ OpenTelemetry tracing initialized: {service_name} -> {jaeger_endpoint}"
    )

    # Return tracer for this service
    return trace.get_tracer(__name__)
