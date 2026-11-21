"""OpenTelemetry tracing bootstrap.

With no OTLP endpoint configured, the global no-op tracer stays in place and
span calls cost almost nothing. In AWS the endpoint is the ADOT collector sidecar.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup_tracing(*, service_name: str, environment: str, otlp_endpoint: str | None) -> TracerProvider | None:
    global _configured
    if otlp_endpoint is None or _configured:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name, "deployment.environment": environment})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    _configured = True
    return provider


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
