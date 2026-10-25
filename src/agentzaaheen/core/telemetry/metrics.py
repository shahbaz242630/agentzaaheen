"""RED metrics made from the spans we already open: rate, errors and duration (ADR-032, 08-OBSERVABILITY §4).

Every finished span adds one observation to a duration histogram, so anything
traced is also counted, without a second set of calls in the code:

* a server span (an API request) → ``http.server.request.duration``,
* a client span (an outside call) → ``http.client.request.duration``,
* any other span → ``agentzaaheen.operation.duration``, named by ``agentzaaheen.operation``.

Rate is the histogram's count, errors are the observations carrying
``error.type``, and duration is its buckets. Each series keeps the last span it
saw as an exemplar, so a slow bucket leads straight to a trace.

Only :data:`METRIC_KEYS` become labels (a bounded set: routes, hosts, methods,
status codes, our reason codes). At most :data:`MAX_SERIES` series exist; a new
one past that is dropped and counted. Values are cumulative since start-up.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, field

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import SpanKind

from agentzaaheen.core.telemetry.encode import Json, attributes, resource, text

MAX_SERIES = 500
# Seconds; the OpenTelemetry advice for HTTP durations.
BOUNDS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)

SERVER = "http.server.request.duration"
CLIENT = "http.client.request.duration"
OPERATION = "agentzaaheen.operation.duration"

METRIC_KEYS: frozenset[str] = frozenset(
    {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "server.address",
        "error.type",
        "db.system.name",
        "agentzaaheen.source",
        "agentzaaheen.project",
        "agentzaaheen.operation",
    }
)

type Labels = tuple[tuple[str, str | int | float | bool], ...]


@dataclass
class _Series:
    counts: list[int] = field(default_factory=lambda: [0] * (len(BOUNDS) + 1))
    total: int = 0
    sum_s: float = 0.0
    exemplar: Json | None = None


class RedMetrics(SpanProcessor):
    """Counts finished spans into histograms. Safe to call from any thread."""

    def __init__(self, start_ns: int) -> None:
        self._start_ns = start_ns
        self._series: dict[tuple[str, Labels], _Series] = {}
        self._lock = threading.Lock()
        self._dropped = 0

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """Nothing to do until the span ends."""

    def on_end(self, span: ReadableSpan) -> None:
        """Add the span's duration to its series."""
        if span.start_time is None or span.end_time is None:
            return
        name, labels = _metric_of(span)
        seconds = max(0, span.end_time - span.start_time) / 1e9
        context = span.context
        with self._lock:
            series = self._series.get((name, labels))
            if series is None:
                if len(self._series) >= MAX_SERIES:
                    self._dropped += 1
                    return
                series = self._series[(name, labels)] = _Series()
            series.counts[bisect_left(BOUNDS, seconds)] += 1
            series.total += 1
            series.sum_s += seconds
            if context is not None and context.is_valid:
                series.exemplar = {
                    "timeUnixNano": str(span.end_time),
                    "asDouble": seconds,
                    "traceId": f"{context.trace_id:032x}",
                    "spanId": f"{context.span_id:016x}",
                }

    def shutdown(self) -> None:
        """Nothing held open."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002  # sent by the pump
        """Nothing to flush here; the pump sends a snapshot."""
        return True

    @property
    def dropped(self) -> int:
        """Spans not counted because :data:`MAX_SERIES` was reached."""
        with self._lock:
            return self._dropped

    def encode(self, resource_values: Mapping[str, object], now_ns: int) -> Json | None:
        """An OTLP/JSON metrics body with every series so far, or ``None`` before the first span."""
        with self._lock:
            snapshot = [
                (name, labels, list(s.counts), s.total, s.sum_s, s.exemplar)
                for (name, labels), s in sorted(self._series.items(), key=lambda item: repr(item[0]))
            ]
        if not snapshot:
            return None
        by_name: dict[str, list[Json]] = {}
        for name, labels, counts, total, sum_s, exemplar in snapshot:
            point: Json = {
                "attributes": attributes(dict(labels), METRIC_KEYS),
                "startTimeUnixNano": str(self._start_ns),
                "timeUnixNano": str(now_ns),
                "count": str(total),
                "sum": sum_s,
                "bucketCounts": [str(c) for c in counts],
                "explicitBounds": list(BOUNDS),
            }
            if exemplar is not None:
                point["exemplars"] = [exemplar]
            by_name.setdefault(name, []).append(point)
        metrics = [
            {
                "name": name,
                "unit": "s",
                "histogram": {"aggregationTemporality": 2, "dataPoints": points},  # 2 = cumulative
            }
            for name, points in by_name.items()
        ]
        return {
            "resourceMetrics": [
                {
                    "resource": resource(resource_values),
                    "scopeMetrics": [{"scope": {"name": "agentzaaheen", "version": ""}, "metrics": metrics}],
                }
            ]
        }


def _metric_of(span: ReadableSpan) -> tuple[str, Labels]:
    found = (span.attributes or {}).items()
    values: dict[str, str | int | float | bool] = {
        k: v for k, v in found if k in METRIC_KEYS and isinstance(v, str | int | float)
    }
    if span.kind is SpanKind.SERVER:
        name = SERVER
    elif span.kind is SpanKind.CLIENT:
        name = CLIENT
    else:
        name = OPERATION
        values["agentzaaheen.operation"] = text(span.name)
    return name, tuple(sorted(values.items()))
