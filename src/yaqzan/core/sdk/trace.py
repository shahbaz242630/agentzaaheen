"""Spans, and the IDs that join a log line to its trace (ADR-032, R2).

Only the OpenTelemetry *API* is used here. Until :mod:`yaqzan.core.telemetry`
installs a provider, every span is a cheap no-op and no ID exists, so code can
open spans freely. Spans go to Agent Zaaheen's own provider, never the global one, so a
library that opens spans of its own (FastAPI does) cannot send anything.

What a span may carry is fixed by :data:`ALLOWED_ATTRIBUTES`: codes, names,
counts and durations, never content (08-OBSERVABILITY §4). The exporter drops
any other key, and exceptions are never recorded (their text can quote data):
a failed span carries only ``error.type``, our own reason code.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from typing import Literal

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

TRACER_NAME = "yaqzan"

type AttributeValue = str | int | float | bool
type Kind = Literal["internal", "server", "client"]

# The only keys a span may carry. Semantic-convention names where one exists.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "server.address",
        "error.type",
        "db.system.name",
        "yaqzan.attempt",
        "yaqzan.attempts",
        "yaqzan.reason",
        "yaqzan.project",
        "yaqzan.source",
        "yaqzan.count",
        "yaqzan.elapsed_ms",
    }
)

_KINDS: Mapping[Kind, SpanKind] = {
    "internal": SpanKind.INTERNAL,
    "server": SpanKind.SERVER,
    "client": SpanKind.CLIENT,
}


class _Installed:
    provider: trace.TracerProvider = trace.NoOpTracerProvider()


def install(provider: trace.TracerProvider | None) -> None:
    """Send Agent Zaaheen's spans to ``provider``; ``None`` switches tracing off again."""
    _Installed.provider = provider if provider is not None else trace.NoOpTracerProvider()


def error_type(exc: BaseException) -> str:
    """Our reason code for ``exc`` (``net.timeout``, ``store.unavailable``), else its class name."""
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str):
        return reason
    code = getattr(exc, "code", None)
    if isinstance(code, Enum) and isinstance(code.value, str):
        return code.value
    return type(exc).__name__


@contextmanager
def span(
    name: str, *, kind: Kind = "internal", attributes: Mapping[str, AttributeValue] | None = None
) -> Iterator[Span]:
    """Open a span around a block. A failure marks it as an error with ``error.type``, then propagates."""
    tracer = trace.get_tracer(TRACER_NAME, tracer_provider=_Installed.provider)
    with tracer.start_as_current_span(
        name,
        kind=_KINDS[kind],
        attributes=dict(attributes or {}),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as exc:
            failed(current, error_type(exc))
            raise


def failed(current: Span, code: str) -> None:
    """Mark ``current`` as failed with our own reason code."""
    current.set_status(Status(StatusCode.ERROR))
    current.set_attribute("error.type", code)


def note(key: str, value: AttributeValue) -> None:
    """Set one attribute on the current span (a no-op outside one)."""
    trace.get_current_span().set_attribute(key, value)


def event(name: str, attributes: Mapping[str, AttributeValue]) -> None:
    """Add a named event (a retry, say) to the current span (a no-op outside one)."""
    trace.get_current_span().add_event(name, dict(attributes))


def trace_ids() -> tuple[str, str] | None:
    """The current trace and span IDs as hex, or ``None`` outside a recorded span."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return trace.format_trace_id(context.trace_id), trace.format_span_id(context.span_id)


def traceparent() -> str | None:
    """The current span as a W3C ``traceparent`` value, or ``None`` outside a recorded span."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    trace_id, span_id = trace.format_trace_id(context.trace_id), trace.format_span_id(context.span_id)
    return f"00-{trace_id}-{span_id}-{int(context.trace_flags):02x}"
