"""Telemetry: Yaqzan's traces, RED metrics and log lines, sent to the store (ADR-032, 08-OBSERVABILITY §4).

What it does:
    :func:`start_tracing` gives Yaqzan its own OpenTelemetry provider, so the
    spans every module opens with ``core.sdk.span`` are batched and sent, as
    OTLP/JSON, through a ``TelemetrySender`` (``core.net.OtlpSender``). The same
    spans become RED metrics (:class:`RedMetrics`: rate, errors, duration, each
    series with a trace exemplar), and every redacted log line is queued
    (:class:`LogQueue`) with only its allow-listed fields.
    :func:`encode_spans` is the last step before sending spans: it keeps only
    the allowed attributes, redacts every string and drops status text.

Public interface:
    ``start_tracing``, ``Tracing``, ``TelemetrySender``, ``encode_spans``,
    ``RedMetrics``, ``LogQueue``, ``LOG_FIELDS``, ``METRIC_KEYS``.

How to test:
    ``uv run pytest tests/unit/test_telemetry.py tests/unit/test_telemetry_red_logs.py``

Must never:
    Send content (only codes, names, counts and durations), slow a request
    down while sending, retry a failed send, or install the global provider
    (a library's own spans would then leave too). Only this module may import
    the OpenTelemetry SDK (import-linter).
"""

from yaqzan.core.telemetry.encode import encode_spans
from yaqzan.core.telemetry.logs import LOG_FIELDS, LogQueue
from yaqzan.core.telemetry.metrics import METRIC_KEYS, RedMetrics
from yaqzan.core.telemetry.tracing import TelemetrySender, Tracing, start_tracing

__all__ = [
    "LOG_FIELDS",
    "METRIC_KEYS",
    "LogQueue",
    "RedMetrics",
    "TelemetrySender",
    "Tracing",
    "encode_spans",
    "start_tracing",
]
