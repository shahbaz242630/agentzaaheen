"""Switch telemetry on: traces, RED metrics and log lines, batched and dropped on failure (ADR-032, R2).

Spans go to Yaqzan's own provider and are sent every few seconds by the
OpenTelemetry batch worker. The same finished spans feed :class:`RedMetrics`,
and every redacted log line is queued by :class:`LogQueue`. One background
pump sends the queued log lines every :data:`LOGS_EVERY_S` seconds and a
metrics snapshot every :data:`METRICS_EVERY_S` (one point per series per
minute: the store's free tier counts points per minute). Nothing here ever
blocks a request: a slow or missing store only means dropped telemetry.
:meth:`Tracing.shutdown` sends what is left and switches everything off.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from yaqzan.core.sdk import install_log_sink, install_tracing
from yaqzan.core.telemetry.encode import Json, encode_spans
from yaqzan.core.telemetry.logs import LogQueue, encode_logs
from yaqzan.core.telemetry.metrics import RedMetrics

SERVICE_NAME = "yaqzan"
EXPORT_EVERY_MS = 5_000
QUEUE_SIZE = 2_048
BATCH_SIZE = 256
LOGS_EVERY_S = 5.0
METRICS_EVERY_S = 60.0


class TelemetrySender(Protocol):
    """Where an encoded batch goes (``core.net.OtlpSender`` in real use)."""

    def send_traces(self, body: bytes) -> bool:
        """Send one batch of spans; True if it was taken."""
        ...

    def send_metrics(self, body: bytes) -> bool:
        """Send one metrics snapshot; True if it was taken."""
        ...

    def send_logs(self, body: bytes) -> bool:
        """Send one batch of log lines; True if it was taken."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


def _body(payload: Json) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


class _Exporter(SpanExporter):
    def __init__(self, sender: TelemetrySender) -> None:
        self._sender = sender

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        ok = self._sender.send_traces(_body(encode_spans(spans)))
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """The sender is closed by :meth:`Tracing.shutdown`, after the last logs and metrics."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002  # nothing is buffered here
        return True


class _Pump:
    """Sends queued log lines and metrics snapshots from one background thread."""

    def __init__(
        self,
        sender: TelemetrySender,
        logs: LogQueue,
        metrics: RedMetrics,
        resource_values: Mapping[str, object],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sender, self._logs, self._metrics = sender, logs, metrics
        self._resource = dict(resource_values)
        self._clock = clock
        self._stop = threading.Event()
        self._last_metrics = clock()
        self._thread = threading.Thread(target=self._run, name="yaqzan-telemetry", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(LOGS_EVERY_S):
            self.tick()

    def tick(self) -> None:
        """Send the queued log lines, and the metrics when a minute has passed."""
        self.send_logs()
        if self._clock() - self._last_metrics >= METRICS_EVERY_S:
            self.send_metrics()

    def send_logs(self) -> None:
        while records := self._logs.take():
            self._sender.send_logs(_body(encode_logs(records, self._resource)))

    def send_metrics(self) -> None:
        self._last_metrics = self._clock()
        payload = self._metrics.encode(self._resource, time.time_ns())
        if payload is not None:
            self._sender.send_metrics(_body(payload))

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()


class Tracing:
    """Telemetry while it is on. Always :meth:`shutdown` before exit, or the last of it is lost."""

    def __init__(self, provider: TracerProvider, pump: _Pump, sender: TelemetrySender) -> None:
        self._provider = provider
        self._pump = pump
        self._sender = sender

    @property
    def provider(self) -> TracerProvider:
        """The provider spans go to."""
        return self._provider

    def shutdown(self) -> None:
        """Switch everything off, send what is left (spans, then logs and metrics) and close the sender."""
        install_tracing(None)
        install_log_sink(None)
        self._pump.stop()
        self._provider.shutdown()
        self._pump.send_logs()
        self._pump.send_metrics()
        self._sender.close()


def start_tracing(sender: TelemetrySender, *, version: str, environment: str = "laptop") -> Tracing:
    """Send every Yaqzan span, its RED metrics and every log line to ``sender`` from now on."""
    resource_values = {
        "service.name": SERVICE_NAME,
        "service.version": version,
        "deployment.environment.name": environment,
    }
    provider = TracerProvider(resource=Resource.create(resource_values), sampler=ALWAYS_ON)
    provider.add_span_processor(
        BatchSpanProcessor(
            _Exporter(sender),
            schedule_delay_millis=EXPORT_EVERY_MS,
            max_queue_size=QUEUE_SIZE,
            max_export_batch_size=BATCH_SIZE,
        )
    )
    metrics = RedMetrics(start_ns=time.time_ns())
    provider.add_span_processor(metrics)
    logs = LogQueue()
    pump = _Pump(sender, logs, metrics, resource_values)
    install_tracing(provider)
    install_log_sink(logs)
    pump.start()
    return Tracing(provider, pump, sender)
