"""Telemetry: what may leave the laptop, in the OTLP/JSON shape, batched and dropped on failure (ADR-032).

Fake secrets are assembled at runtime so the secret scanner never sees a
token-shaped string in this file.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode

from tests.tracing import Recorded, recorded
from yaqzan.core.sdk import ALLOWED_ATTRIBUTES, span
from yaqzan.core.telemetry import encode_spans, start_tracing
from yaqzan.core.telemetry.encode import MAX_TEXT, attributes
from yaqzan.core.telemetry.tracing import _Exporter

FAKE_TOKEN = "gh" + "p_" + "A" * 36


@pytest.fixture
def spans() -> Iterator[Recorded]:
    with recorded() as kept:
        yield kept


def only_span(body: dict[str, object]) -> Any:  # walking a JSON body
    tree: Any = body
    [resource] = tree["resourceSpans"]
    [scope] = resource["scopeSpans"]
    [one] = scope["spans"]
    return one


def as_dict(pairs: object) -> dict[str, object]:
    return {p["key"]: p["value"] for p in pairs}  # type: ignore[attr-defined]


def test_a_span_is_encoded_in_the_otlp_json_shape(spans: Recorded) -> None:
    with (
        span("outer"),
        span("GET api.example.com", kind="client", attributes={"server.address": "api.example.com"}),
    ):
        pass
    inner = spans.named("GET api.example.com")
    encoded = only_span(encode_spans([inner]))
    assert inner.context is not None
    assert inner.parent is not None
    assert encoded["traceId"] == f"{inner.context.trace_id:032x}"
    assert encoded["spanId"] == f"{inner.context.span_id:016x}"
    assert encoded["parentSpanId"] == f"{inner.parent.span_id:016x}"
    assert encoded["kind"] == 3  # SPAN_KIND_CLIENT
    assert encoded["startTimeUnixNano"] == str(inner.start_time)
    assert encoded["endTimeUnixNano"] == str(inner.end_time)
    assert encoded["status"] == {"code": 0}
    assert as_dict(encoded["attributes"]) == {"server.address": {"stringValue": "api.example.com"}}
    assert "events" not in encoded
    json.dumps(encode_spans(spans.spans), allow_nan=False)  # the whole body is plain JSON


def test_a_root_span_has_no_parent(spans: Recorded) -> None:
    with span("root"):
        pass
    assert "parentSpanId" not in only_span(encode_spans(spans.spans))


def test_only_allowed_attributes_leave(spans: Recorded) -> None:
    with span("work") as current:
        current.set_attribute("yaqzan.count", 3)
        current.set_attribute("customer.email", "someone@example.com")
        current.set_attribute("url.full", "https://api.example.com/x?token=abc")
    keys = set(as_dict(only_span(encode_spans(spans.spans))["attributes"]))
    assert keys == {"yaqzan.count"}


def test_strings_are_redacted_and_cut(spans: Recorded) -> None:
    with span(f"named {FAKE_TOKEN}") as current:
        current.set_attribute("yaqzan.reason", f"bearer {FAKE_TOKEN}")
        current.set_attribute("yaqzan.source", "x" * 1000)
    encoded = only_span(encode_spans(spans.spans))
    text = json.dumps(encoded)
    assert FAKE_TOKEN not in text
    values = as_dict(encoded["attributes"])
    assert values["yaqzan.source"] == {"stringValue": "x" * MAX_TEXT}
    assert "[REDACTED]" in str(encoded["name"])


def test_a_secret_split_by_the_cut_is_still_redacted() -> None:
    pairs = attributes({"yaqzan.reason": "x" * (MAX_TEXT - 10) + FAKE_TOKEN}, ALLOWED_ATTRIBUTES)
    assert "AAAAAAAA" not in json.dumps(pairs)


def test_each_kind_of_value_is_encoded_and_others_dropped() -> None:
    values = {
        "yaqzan.count": 7,
        "yaqzan.elapsed_ms": 1.5,
        "yaqzan.attempt": True,
        "yaqzan.reason": "ok",
        "yaqzan.source": ("a", "b"),
        "yaqzan.project": math.nan,
    }
    assert as_dict(attributes(values, ALLOWED_ATTRIBUTES)) == {
        "yaqzan.count": {"intValue": "7"},
        "yaqzan.elapsed_ms": {"doubleValue": 1.5},
        "yaqzan.attempt": {"boolValue": True},
        "yaqzan.reason": {"stringValue": "ok"},
    }


def test_a_failed_span_keeps_its_code_never_its_text(spans: Recorded) -> None:
    with span("work") as current:
        current.set_status(Status(StatusCode.ERROR, f"leaked {FAKE_TOKEN}"))
    encoded = only_span(encode_spans(spans.spans))
    assert encoded["status"] == {"code": 2}


def test_events_keep_their_name_and_allowed_attributes(spans: Recorded) -> None:
    with span("work") as current:
        current.add_event("retry", {"yaqzan.attempt": 2, "body": "the vendor said no"})
    [encoded_event] = only_span(encode_spans(spans.spans))["events"]
    assert encoded_event["name"] == "retry"
    assert as_dict(encoded_event["attributes"]) == {"yaqzan.attempt": {"intValue": "2"}}


def test_only_our_resource_keys_leave() -> None:
    exporter = InMemorySpanExporter()
    resource = Resource.create({"service.name": "yaqzan", "host.name": "the-laptop"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("s"):
        pass
    body: Any = encode_spans(exporter.get_finished_spans())
    [entry] = body["resourceSpans"]
    assert as_dict(entry["resource"]["attributes"]) == {"service.name": {"stringValue": "yaqzan"}}
    assert entry["scopeSpans"][0]["scope"] == {"name": "t", "version": ""}


@pytest.mark.parametrize(
    ("kind", "number"),
    [
        (SpanKind.INTERNAL, 1),
        (SpanKind.SERVER, 2),
        (SpanKind.CLIENT, 3),
        (SpanKind.PRODUCER, 4),
        (SpanKind.CONSUMER, 5),
    ],
)
def test_every_kind_has_its_otlp_number(kind: SpanKind, number: int) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("s", kind=kind):
        pass
    assert only_span(encode_spans(exporter.get_finished_spans()))["kind"] == number


def test_an_unfinished_or_invalid_span_is_skipped() -> None:
    unfinished = ReadableSpan(name="open", context=SpanContext(1, 2, is_remote=False), start_time=1)
    invalid = ReadableSpan(name="bad", context=SpanContext(0, 0, is_remote=False), start_time=1, end_time=2)
    assert encode_spans([unfinished, invalid]) == {"resourceSpans": []}
    assert encode_spans([]) == {"resourceSpans": []}


class FakeSender:
    def __init__(self, *, accept: bool = True) -> None:
        self.bodies: list[bytes] = []
        self.accept = accept
        self.closed = False

    def send_traces(self, body: bytes) -> bool:
        self.bodies.append(body)
        return self.accept

    def close(self) -> None:
        self.closed = True


def test_start_tracing_sends_every_span_on_shutdown() -> None:
    sender = FakeSender()
    tracing = start_tracing(sender, version="9.9.9")
    with span("request", kind="server"):
        pass
    tracing.shutdown()
    assert sender.closed
    [body] = sender.bodies
    sent = json.loads(body)
    [entry] = sent["resourceSpans"]
    assert as_dict(entry["resource"]["attributes"]) == {
        "deployment.environment.name": {"stringValue": "laptop"},
        "service.name": {"stringValue": "yaqzan"},
        "service.version": {"stringValue": "9.9.9"},
    }
    assert entry["scopeSpans"][0]["spans"][0]["name"] == "request"
    with span("after shutdown") as current:
        assert not current.is_recording()


def test_a_refused_send_is_dropped_quietly() -> None:
    sender = FakeSender(accept=False)
    tracing = start_tracing(sender, version="1")
    with span("request"):
        pass
    tracing.shutdown()  # no exception: telemetry never breaks Yaqzan
    assert len(sender.bodies) == 1
    assert tracing.provider is not None


def test_the_exporter_holds_nothing_to_flush() -> None:
    assert _Exporter(FakeSender()).force_flush() is True
