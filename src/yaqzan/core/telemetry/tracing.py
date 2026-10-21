"""Switch tracing on: Yaqzan's own provider, batched export, dropped on failure (ADR-032, R2).

Spans are batched in memory and sent every few seconds from a background
thread, so a slow or missing trace store never slows a request. When the queue
is full, new spans are dropped. Every span is kept (no sampling): the laptop
makes few, far under the store's free limits. :meth:`Tracing.shutdown` sends
what is left and switches tracing off again.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from yaqzan.core.sdk import install_tracing
from yaqzan.core.telemetry.encode import encode_spans

SERVICE_NAME = "yaqzan"
EXPORT_EVERY_MS = 5_000
QUEUE_SIZE = 2_048
BATCH_SIZE = 256


class TraceSender(Protocol):
    """Where an encoded batch goes (``core.net.OtlpSender`` in real use)."""

    def send_traces(self, body: bytes) -> bool:
        """Send one batch; True if it was taken."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


class _Exporter(SpanExporter):
    def __init__(self, sender: TraceSender) -> None:
        self._sender = sender

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        body = json.dumps(encode_spans(spans), separators=(",", ":"), allow_nan=False).encode()
        return SpanExportResult.SUCCESS if self._sender.send_traces(body) else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._sender.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002  # nothing is buffered here
        return True


class Tracing:
    """Tracing while it is on. Always :meth:`shutdown` before exit, or the last spans are lost."""

    def __init__(self, provider: TracerProvider) -> None:
        self._provider = provider

    @property
    def provider(self) -> TracerProvider:
        """The provider spans go to."""
        return self._provider

    def shutdown(self) -> None:
        """Send what is queued, switch tracing off and close the sender."""
        install_tracing(None)
        self._provider.shutdown()


def start_tracing(sender: TraceSender, *, version: str, environment: str = "laptop") -> Tracing:
    """Send every Yaqzan span to ``sender`` from now on."""
    resource = Resource.create(
        {"service.name": SERVICE_NAME, "service.version": version, "deployment.environment.name": environment}
    )
    provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)
    provider.add_span_processor(
        BatchSpanProcessor(
            _Exporter(sender),
            schedule_delay_millis=EXPORT_EVERY_MS,
            max_queue_size=QUEUE_SIZE,
            max_export_batch_size=BATCH_SIZE,
        )
    )
    install_tracing(provider)
    return Tracing(provider)
