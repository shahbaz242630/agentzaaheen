"""Telemetry: Yaqzan's traces, sent to the trace store (ADR-032, 08-OBSERVABILITY §4).

What it does:
    :func:`start_tracing` gives Yaqzan its own OpenTelemetry provider, so the
    spans every module opens with ``core.sdk.span`` are batched and sent, as
    OTLP/JSON, through a ``TraceSender`` (``core.net.OtlpSender``).
    :func:`encode_spans` is the last step before sending: it keeps only the
    allowed attributes, redacts every string and drops status text.

Public interface:
    ``start_tracing``, ``Tracing``, ``TraceSender``, ``encode_spans``.

How to test:
    ``uv run pytest tests/unit/test_telemetry.py tests/unit/test_sdk_trace.py``

Must never:
    Send content (only codes, names, counts and durations), slow a request
    down while sending, retry a failed send, or install the global provider
    (a library's own spans would then leave too). Only this module may import
    the OpenTelemetry SDK (import-linter).
"""

from yaqzan.core.telemetry.encode import encode_spans
from yaqzan.core.telemetry.tracing import TraceSender, Tracing, start_tracing

__all__ = ["TraceSender", "Tracing", "encode_spans", "start_tracing"]
