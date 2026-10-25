"""Record Agent Zaaheen's spans in memory for a test (no network, nothing global changed)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentzaaheen.core.sdk import install_tracing


class Recorded:
    """The spans finished so far, oldest first."""

    def __init__(self, exporter: InMemorySpanExporter, provider: TracerProvider) -> None:
        self._exporter = exporter
        self.provider = provider

    @property
    def spans(self) -> tuple[ReadableSpan, ...]:
        return tuple(self._exporter.get_finished_spans())

    def named(self, name: str) -> ReadableSpan:
        found = [s for s in self.spans if s.name == name]
        assert len(found) == 1, f"expected one span named {name!r}, got {[s.name for s in self.spans]}"
        return found[0]


@contextmanager
def recorded() -> Iterator[Recorded]:
    """Install a provider that keeps every span in memory; switch tracing off afterwards."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    install_tracing(provider)
    try:
        yield Recorded(exporter, provider)
    finally:
        install_tracing(None)
        provider.shutdown()
