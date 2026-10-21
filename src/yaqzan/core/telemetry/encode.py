"""Finished spans as an OTLP/JSON body, keeping only what may leave the laptop (08-OBSERVABILITY §4).

The allow-list is enforced here, at the last step before sending: an attribute
whose key is not in :data:`yaqzan.core.sdk.ALLOWED_ATTRIBUTES` is dropped, every
string is redacted and cut short, a status keeps its code but never its text,
and span events keep their name and allowed attributes only. A mistake in a
caller can therefore lose a detail, but cannot send content.

Format: the OTLP/JSON mapping of ``ExportTraceServiceRequest`` (IDs as hex,
times and 64-bit integers as strings, enum values as numbers).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

from yaqzan.core.sdk import ALLOWED_ATTRIBUTES, redact_text

MAX_TEXT = 256
# Resource keys we set ourselves at start-up; anything else a library adds is dropped.
RESOURCE_KEYS = frozenset({"service.name", "service.version", "deployment.environment.name"})

_OTLP_KIND = {
    SpanKind.INTERNAL: 1,
    SpanKind.SERVER: 2,
    SpanKind.CLIENT: 3,
    SpanKind.PRODUCER: 4,
    SpanKind.CONSUMER: 5,
}

type Json = dict[str, object]


def encode_spans(spans: Sequence[ReadableSpan]) -> Json:
    """One OTLP/JSON request body for ``spans`` (grouped by resource and scope)."""
    resources: dict[int, tuple[Json, dict[tuple[str, str], list[Json]]]] = {}
    for item in spans:
        owner = item.resource
        entry = resources.setdefault(id(owner), (resource(owner.attributes), {}))
        scope = item.instrumentation_scope
        key = (scope.name, scope.version or "") if scope is not None else ("", "")
        encoded = _span(item)
        if encoded is not None:
            entry[1].setdefault(key, []).append(encoded)
    return {
        "resourceSpans": [
            {
                "resource": encoded_resource,
                "scopeSpans": [
                    {"scope": {"name": name, "version": version}, "spans": encoded}
                    for (name, version), encoded in scopes.items()
                ],
            }
            for encoded_resource, scopes in resources.values()
            if scopes
        ]
    }


def _span(item: ReadableSpan) -> Json | None:
    context = item.context
    if context is None or not context.is_valid or item.start_time is None or item.end_time is None:
        return None
    out: Json = {
        "traceId": f"{context.trace_id:032x}",
        "spanId": f"{context.span_id:016x}",
        "name": text(item.name),
        "kind": _OTLP_KIND.get(item.kind, 1),
        "startTimeUnixNano": str(item.start_time),
        "endTimeUnixNano": str(item.end_time),
        "attributes": attributes(item.attributes or {}, ALLOWED_ATTRIBUTES),
        "status": {"code": int(item.status.status_code.value)},
    }
    if item.parent is not None and item.parent.is_valid:
        out["parentSpanId"] = f"{item.parent.span_id:016x}"
    events = [
        {
            "timeUnixNano": str(event.timestamp),
            "name": text(event.name),
            "attributes": attributes(event.attributes or {}, ALLOWED_ATTRIBUTES),
        }
        for event in item.events
    ]
    if events:
        out["events"] = events
    return out


def resource(values: Mapping[str, object]) -> Json:
    """An OTLP resource holding only :data:`RESOURCE_KEYS`."""
    return {"attributes": attributes(values, RESOURCE_KEYS)}


def attributes(values: Mapping[str, object], allowed: frozenset[str]) -> list[Json]:
    """The allowed keys as OTLP key/value pairs, sorted; any other key or kind of value is dropped."""
    out: list[Json] = []
    for key in sorted(values):
        if key not in allowed:
            continue
        value = _value(values[key])
        if value is not None:
            out.append({"key": key, "value": value})
    return out


def _value(value: object) -> Json | None:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value} if math.isfinite(value) else None  # NaN is not JSON
    if isinstance(value, str):
        return {"stringValue": text(value)}
    return None  # lists and anything else: not part of our contract


def text(value: str) -> str:
    """``value`` redacted, then cut to :data:`MAX_TEXT`."""
    # Redact before cutting: a cut could split a secret so its pattern no longer matches.
    return redact_text(value)[:MAX_TEXT]
