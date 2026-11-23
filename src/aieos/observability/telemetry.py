"""OpenTelemetry tracing bootstrap and the span helper used across the platform.

With no OTLP endpoint configured, the global no-op tracer stays in place and
span calls cost almost nothing. In AWS the endpoint is the ADOT collector sidecar.

What a trace looks like: API request -> Temporal workflow (context carried in Temporal
headers by the tracing interceptor) -> `run_agent_step` activity -> `agent.step` ->
`chat <model>` per model call and `execute_tool <name>` per tool call -> the tool-call
child workflow -> `gateway <tool>` in the gateway worker.

Spans carry identifiers, sizes, token counts, cost and outcomes. They never carry prompts,
model output, tool arguments or tool results (those can hold code, secrets or personal
data), and errors are recorded by exception type only, without the message.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.util.types import AttributeValue

_configured = False

TRACER_NAME = "aieos"


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


def tracer() -> Tracer:
    # A proxy: it follows whatever provider is installed later (or stays a no-op).
    return trace.get_tracer(TRACER_NAME)


def _clean(attributes: Mapping[str, Any]) -> dict[str, AttributeValue]:
    out: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        out[key] = value if isinstance(value, bool | int | float | str) else str(value)
    return out


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """A span that records failures by exception type only (messages can echo prompts or secrets)."""
    with tracer().start_as_current_span(
        name, attributes=_clean(attributes), record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException as exc:
            current.set_attribute("error.type", type(exc).__qualname__)
            current.set_status(Status(StatusCode.ERROR, type(exc).__qualname__))
            raise


def set_attributes(current: Span, **attributes: Any) -> None:
    current.set_attributes(_clean(attributes))


def mark_error(current: Span, error_type: str) -> None:
    """For failures that are handled (not raised through the span): type only, never the message."""
    current.set_attribute("error.type", error_type)
    current.set_status(Status(StatusCode.ERROR, error_type))


def temporal_interceptors(tracer_: Tracer | None = None) -> list[Any]:
    """Carry the trace context through Temporal: client -> workflow -> activities -> child workflows.

    Given to the client, it also applies to every worker built from that client."""
    from temporalio.contrib.opentelemetry import TracingInterceptor

    return [TracingInterceptor(tracer_)]
