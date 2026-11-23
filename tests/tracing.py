"""Capture spans in tests: one in-memory provider for the whole process (the global provider can be set once)."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter = InMemorySpanExporter()
_provider: TracerProvider | None = None


def provider() -> TracerProvider:
    global _provider
    if _provider is None:
        _provider = TracerProvider()
        _provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(_provider)
    return _provider


class SpanCapture:
    def __enter__(self) -> SpanCapture:
        provider()
        _exporter.clear()
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    @property
    def spans(self) -> list[ReadableSpan]:
        return list(_exporter.get_finished_spans())

    def named(self, prefix: str) -> list[ReadableSpan]:
        return [s for s in self.spans if s.name.startswith(prefix)]


def all_attribute_text(spans: list[ReadableSpan]) -> str:
    """Every attribute value and status text, to assert that no content leaked into a trace."""
    parts: list[str] = []
    for s in spans:
        parts.append(s.name)
        parts.extend(str(v) for v in (s.attributes or {}).values())
        parts.append(s.status.description or "")
        parts.extend(e.name for e in s.events)
    return "\n".join(parts)
