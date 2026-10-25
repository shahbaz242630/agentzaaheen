"""Spans and the IDs that join logs to traces (ADR-032, R2).

Without a provider every span is a no-op and no ID exists; with one, spans carry
only what the caller set, a failure carries our reason code and never the
exception text, and every log line inside a span carries its IDs.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from agentzaaheen.core.net import NetError, NetErrorCode
from agentzaaheen.core.sdk import error_type, event, failed, get_logger, note, span, trace_ids, traceparent
from agentzaaheen.core.store import StoreError, StoreErrorCode
from tests.tracing import Recorded, recorded

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@pytest.fixture
def spans() -> Iterator[Recorded]:
    with recorded() as kept:
        yield kept


def test_without_a_provider_spans_are_no_ops_with_no_ids() -> None:
    with span("nothing", attributes={"agentzaaheen.count": 1}) as current:
        note("agentzaaheen.count", 2)
        event("retry", {"agentzaaheen.attempt": 1})
        assert not current.is_recording()
        assert trace_ids() is None
        assert traceparent() is None


def test_a_span_keeps_its_name_kind_and_attributes(spans: Recorded) -> None:
    with span("GET api.example.com", kind="client", attributes={"server.address": "api.example.com"}):
        note("http.response.status_code", 200)
        event("retry", {"agentzaaheen.attempt": 1, "agentzaaheen.reason": "http_503"})
    done = spans.named("GET api.example.com")
    assert done.kind is SpanKind.CLIENT
    assert dict(done.attributes or {}) == {
        "server.address": "api.example.com",
        "http.response.status_code": 200,
    }
    assert [(e.name, dict(e.attributes or {})) for e in done.events] == [
        ("retry", {"agentzaaheen.attempt": 1, "agentzaaheen.reason": "http_503"})
    ]
    assert done.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize(("kind", "expected"), [("internal", SpanKind.INTERNAL), ("server", SpanKind.SERVER)])
def test_every_kind_maps(spans: Recorded, kind: str, expected: SpanKind) -> None:
    with span("one", kind=kind):  # type: ignore[arg-type]  # the kinds, as strings
        pass
    assert spans.named("one").kind is expected


def test_nested_spans_share_one_trace(spans: Recorded) -> None:
    with span("outer"), span("inner"):
        pass
    outer, inner = spans.named("outer"), spans.named("inner")
    assert inner.context is not None
    assert outer.context is not None
    assert inner.context.trace_id == outer.context.trace_id
    assert inner.parent is not None
    assert inner.parent.span_id == outer.context.span_id


def test_a_failure_carries_our_code_and_never_the_exception_text(spans: Recorded) -> None:
    secretish = "the row held alice@example.com"
    with pytest.raises(NetError), span("call"):
        raise NetError(NetErrorCode.TIMEOUT, secretish)
    done = spans.named("call")
    assert done.status.status_code is StatusCode.ERROR
    assert done.status.description is None
    assert dict(done.attributes or {}) == {"error.type": "net.timeout"}
    assert done.events == ()  # the exception is never recorded
    assert secretish not in repr(done.to_json())


def test_failed_marks_a_span_with_a_code(spans: Recorded) -> None:
    with span("call") as current:
        failed(current, "500")
    done = spans.named("call")
    assert done.status.status_code is StatusCode.ERROR
    assert (done.attributes or {})["error.type"] == "500"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (NetError(NetErrorCode.AUTH, "no"), "net.auth"),
        (StoreError(StoreErrorCode.UNAVAILABLE, "down"), "store.unavailable"),
        (ValueError("x"), "ValueError"),
    ],
)
def test_error_type_prefers_our_reason_codes(exc: Exception, code: str) -> None:
    assert error_type(exc) == code


def test_error_type_ignores_a_code_that_is_not_a_text_enum() -> None:
    class OddError(Exception):
        code = 7

    assert error_type(OddError()) == "OddError"


def test_traceparent_names_the_current_span(spans: Recorded) -> None:
    with span("request"):
        header, ids = traceparent(), trace_ids()
    assert header is not None
    assert ids is not None
    match = TRACEPARENT.fullmatch(header)
    assert match is not None
    assert (match.group(1), match.group(2)) == ids
    assert int(match.group(3), 16) & 1  # sampled: the store keeps it
    done = spans.named("request")
    assert done.context is not None
    assert ids == (f"{done.context.trace_id:032x}", f"{done.context.span_id:016x}")


def test_log_lines_inside_a_span_carry_its_ids(spans: Recorded) -> None:
    sink = io.StringIO()
    log = get_logger("test", sink=sink)
    log.info("outside")
    with span("work"):
        log.info("inside")
        ids = trace_ids()
    outside, inside = (json.loads(line) for line in sink.getvalue().splitlines())
    assert "trace_id" not in outside
    assert "span_id" not in outside
    assert ids is not None
    assert (inside["trace_id"], inside["span_id"]) == ids
    assert spans.named("work")


def test_spans_never_reach_the_global_provider(spans: Recorded) -> None:
    # A library using the global API (FastAPI does) gets no recording tracer from us.
    with trace.get_tracer("some.library").start_as_current_span("theirs") as theirs:
        assert not theirs.is_recording()
    with span("ours"):
        pass
    assert [s.name for s in spans.spans] == ["ours"]


def test_switching_off_makes_spans_no_ops_again() -> None:
    with recorded():
        pass
    with span("after") as current:
        assert not current.is_recording()
