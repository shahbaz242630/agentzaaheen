"""RED metrics from spans, and log lines shipped with only their allowed fields (ADR-032, step 3b).

Fake secrets are assembled at runtime so the secret scanner never sees a
token-shaped string in this file.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import SpanContext, SpanKind

from yaqzan.core.sdk import get_logger, install_log_sink, install_tracing, span
from yaqzan.core.telemetry import LOG_FIELDS, LogQueue, RedMetrics, start_tracing
from yaqzan.core.telemetry import tracing as tracing_module
from yaqzan.core.telemetry.encode import MAX_TEXT
from yaqzan.core.telemetry.logs import BATCH_SIZE, encode_logs, encode_record
from yaqzan.core.telemetry.metrics import BOUNDS, CLIENT, MAX_SERIES, OPERATION, SERVER
from yaqzan.core.telemetry.tracing import METRICS_EVERY_S, _Pump

FAKE_TOKEN = "gh" + "p_" + "A" * 36
TRACE_ID = "ab" * 16
SPAN_ID = "cd" * 8
RESOURCE = {"service.name": "yaqzan", "host.name": "the-laptop"}


def as_dict(pairs: Any) -> dict[str, Any]:  # walking a JSON body
    return {p["key"]: p["value"] for p in pairs}


class FakeSender:
    def __init__(self) -> None:
        self.traces: list[bytes] = []
        self.metrics: list[bytes] = []
        self.logs: list[bytes] = []
        self.closed = False
        self.order: list[str] = []

    def send_traces(self, body: bytes) -> bool:
        self.traces.append(body)
        return True

    def send_metrics(self, body: bytes) -> bool:
        self.metrics.append(body)
        self.order.append("metrics")
        return True

    def send_logs(self, body: bytes) -> bool:
        self.logs.append(body)
        self.order.append("logs")
        return True

    def close(self) -> None:
        self.closed = True
        self.order.append("close")

    def log_records(self) -> list[Any]:
        return [
            record
            for body in self.logs
            for entry in json.loads(body)["resourceLogs"]
            for scope in entry["scopeLogs"]
            for record in scope["logRecords"]
        ]


# --- the logger's sink -----------------------------------------------------------------------------


@pytest.fixture
def sink() -> Iterator[list[dict[str, object]]]:
    lines: list[dict[str, object]] = []
    install_log_sink(lambda line: lines.append(dict(line)))
    yield lines
    install_log_sink(None)


def test_the_sink_gets_each_line_after_redaction(sink: list[dict[str, object]]) -> None:
    out = io.StringIO()
    get_logger("t", sink=out).info("hello", reason=f"bearer {FAKE_TOKEN}", count_items=3)
    [line] = sink
    assert line["event"] == "hello"
    assert line["component"] == "t"
    assert FAKE_TOKEN not in json.dumps(line)
    assert "hello" in out.getvalue()


def test_a_failing_sink_never_breaks_logging() -> None:
    def broken(_line: object) -> None:
        raise RuntimeError("store gone")

    install_log_sink(broken)
    try:
        out = io.StringIO()
        get_logger("t", sink=out).info("still_written")
        assert "still_written" in out.getvalue()
    finally:
        install_log_sink(None)


def test_no_sink_once_removed(sink: list[dict[str, object]]) -> None:
    install_log_sink(None)
    get_logger("t", sink=io.StringIO()).info("quiet")
    assert sink == []


# --- log records -----------------------------------------------------------------------------------


def test_only_allowed_fields_leave() -> None:
    line = {
        "event": "chat_tool_used",
        "level": "info",
        "component": "yaqzan.chat",
        "tool": "revenue",
        "bytes": 12,
        "path": "C:/Users/someone/secret.txt",
        "arg": "what did customer x pay",
        "tenant_id": "t-123",
        "error": "boom with details",
        "exception": "Traceback ...",
        "timestamp": "2026-10-21T00:00:00Z",
    }
    record = encode_record(line, 7)
    assert set(as_dict(record["attributes"])) == {"component", "tool", "bytes"}
    assert record["body"] == {"stringValue": "chat_tool_used"}
    assert record["timeUnixNano"] == "7"
    assert "traceId" not in record


@pytest.mark.parametrize(
    ("level", "number"),
    [("debug", 5), ("info", 9), ("warning", 13), ("error", 17), ("critical", 21), ("odd", 9)],
)
def test_levels_become_severities(level: str, number: int) -> None:
    record = encode_record({"event": "e", "level": level}, 1)
    assert record["severityNumber"] == number
    assert record["severityText"] == level.upper()


def test_a_line_without_a_level_is_info() -> None:
    assert encode_record({"event": "e"}, 1)["severityNumber"] == 9


def test_trace_ids_join_the_line_to_its_trace() -> None:
    record = encode_record({"event": "e", "trace_id": TRACE_ID, "span_id": SPAN_ID}, 1)
    assert (record["traceId"], record["spanId"]) == (TRACE_ID, SPAN_ID)


@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [(TRACE_ID, "x" * 16), ("x" * 32, SPAN_ID), (TRACE_ID, None), (TRACE_ID.upper(), SPAN_ID)],
)
def test_odd_trace_ids_are_left_out(trace_id: str, span_id: str | None) -> None:
    record = encode_record({"event": "e", "trace_id": trace_id, "span_id": span_id}, 1)
    assert "traceId" not in record
    assert "spanId" not in record


def test_strings_are_redacted_and_cut() -> None:
    record = encode_record({"event": "x" * 1000, "reason": f"bearer {FAKE_TOKEN}"}, 1)
    assert record["body"] == {"stringValue": "x" * MAX_TEXT}
    assert FAKE_TOKEN not in json.dumps(record)


def test_the_allow_list_is_codes_and_counts_only() -> None:
    assert not {"path", "arg", "tenant_id", "error", "exception", "sha256", "question"} & LOG_FIELDS


def test_the_queue_skips_its_own_drop_reports_and_keeps_order() -> None:
    queue = LogQueue()
    queue({"event": "first"})
    queue({"event": "telemetry_dropped", "reason": "net.timeout"})
    queue({"event": "second"})
    assert len(queue) == 2
    assert [r["body"]["stringValue"] for r in queue.take()] == ["first", "second"]  # type: ignore[index]
    assert queue.take() == []


def test_a_full_queue_drops_new_lines_and_counts_them() -> None:
    queue = LogQueue(size=2)
    for n in range(5):
        queue({"event": f"e{n}"})
    assert len(queue) == 2
    assert queue.dropped == 3
    assert [r["body"]["stringValue"] for r in queue.take(limit=1)] == ["e0"]  # type: ignore[index]
    assert len(queue) == 1


def test_logs_body_shape() -> None:
    assert encode_logs([], RESOURCE) == {"resourceLogs": []}
    body: Any = encode_logs([encode_record({"event": "e"}, 1)], RESOURCE)
    [entry] = body["resourceLogs"]
    assert as_dict(entry["resource"]["attributes"]) == {"service.name": {"stringValue": "yaqzan"}}
    assert entry["scopeLogs"][0]["scope"]["name"] == "yaqzan"
    assert len(entry["scopeLogs"][0]["logRecords"]) == 1


# --- RED metrics -----------------------------------------------------------------------------------


def finished(
    name: str,
    seconds: float,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
    span_id: int = 2,
) -> ReadableSpan:
    return ReadableSpan(
        name=name,
        context=SpanContext(1, span_id, is_remote=False),
        kind=kind,
        attributes=attributes or {},
        start_time=1_000,
        end_time=1_000 + round(seconds * 1e9),
    )


def points(red: RedMetrics, now_ns: int = 99) -> dict[str, list[Any]]:
    body: Any = red.encode(RESOURCE, now_ns)
    assert body is not None
    json.dumps(body, allow_nan=False)
    [entry] = body["resourceMetrics"]
    assert as_dict(entry["resource"]["attributes"]) == {"service.name": {"stringValue": "yaqzan"}}
    [scope] = entry["scopeMetrics"]
    out: dict[str, list[Any]] = {}
    for metric in scope["metrics"]:
        assert metric["unit"] == "s"
        assert metric["histogram"]["aggregationTemporality"] == 2
        out[metric["name"]] = metric["histogram"]["dataPoints"]
    return out


def test_nothing_to_send_before_the_first_span() -> None:
    assert RedMetrics(0).encode(RESOURCE, 1) is None


def test_each_kind_of_span_gets_its_metric() -> None:
    red = RedMetrics(start_ns=5)
    server_attrs = {
        "http.request.method": "GET",
        "http.route": "/api/projects",
        "http.response.status_code": 200,
    }
    red.on_end(finished("GET /api/projects", 0.02, kind=SpanKind.SERVER, attributes=server_attrs))
    client_attrs = {"server.address": "api.paddle.com", "yaqzan.attempts": 2, "yaqzan.count": 9}
    red.on_end(finished("GET api.paddle.com", 0.3, kind=SpanKind.CLIENT, attributes=client_attrs))
    red.on_end(finished("db transaction", 0.001, attributes={"db.system.name": "postgresql"}))
    got = points(red)
    assert set(got) == {SERVER, CLIENT, OPERATION}
    [server] = got[SERVER]
    assert as_dict(server["attributes"]) == {
        "http.request.method": {"stringValue": "GET"},
        "http.response.status_code": {"intValue": "200"},
        "http.route": {"stringValue": "/api/projects"},
    }
    assert server["startTimeUnixNano"] == "5"
    assert server["timeUnixNano"] == "99"
    [client] = got[CLIENT]
    assert as_dict(client["attributes"]) == {"server.address": {"stringValue": "api.paddle.com"}}  # no counts
    [operation] = got[OPERATION]
    assert as_dict(operation["attributes"]) == {
        "db.system.name": {"stringValue": "postgresql"},
        "yaqzan.operation": {"stringValue": "db transaction"},
    }


def test_durations_land_in_upper_inclusive_buckets() -> None:
    red = RedMetrics(0)
    for seconds in (BOUNDS[0], BOUNDS[0] + 0.001, 0.0, 99.0):
        red.on_end(finished("work", seconds))
    [point] = points(red)[OPERATION]
    counts = [int(c) for c in point["bucketCounts"]]
    assert len(counts) == len(BOUNDS) + 1
    assert point["explicitBounds"] == list(BOUNDS)
    assert counts[0] == 2  # 0 s and exactly the first bound
    assert counts[1] == 1
    assert counts[-1] == 1  # past the last bound
    assert point["count"] == "4"
    assert point["sum"] == pytest.approx(BOUNDS[0] * 2 + 0.001 + 99.0)


def test_errors_are_their_own_series_with_a_trace_exemplar() -> None:
    red = RedMetrics(0)
    red.on_end(finished("sync revenue paddle", 0.1, span_id=3))
    red.on_end(finished("sync revenue paddle", 0.2, attributes={"error.type": "net.timeout"}, span_id=4))
    by_error = {
        as_dict(p["attributes"]).get("error.type", {}).get("stringValue"): p for p in points(red)[OPERATION]
    }
    assert set(by_error) == {None, "net.timeout"}
    [exemplar] = by_error["net.timeout"]["exemplars"]
    assert exemplar["traceId"] == f"{1:032x}"
    assert exemplar["spanId"] == f"{4:016x}"
    assert exemplar["asDouble"] == pytest.approx(0.2)


def test_the_series_are_capped() -> None:
    red = RedMetrics(0)
    for n in range(MAX_SERIES + 3):
        red.on_end(finished(f"op {n}", 0.01))
    assert len(points(red)[OPERATION]) == MAX_SERIES
    assert red.dropped == 3
    red.on_end(finished("op 0", 0.01))  # an existing series still counts
    assert red.dropped == 3


def test_an_unfinished_or_idless_span_is_handled() -> None:
    red = RedMetrics(0)
    red.on_end(ReadableSpan(name="open", context=SpanContext(1, 2, is_remote=False), start_time=1))
    assert red.encode(RESOURCE, 1) is None
    red.on_end(
        ReadableSpan(name="no ids", context=SpanContext(0, 0, is_remote=False), start_time=1, end_time=2)
    )
    [point] = points(red)[OPERATION]
    assert "exemplars" not in point


def test_red_metrics_follow_real_spans() -> None:
    red = RedMetrics(0)
    provider = TracerProvider()
    provider.add_span_processor(red)
    install_tracing(provider)
    try:
        with pytest.raises(TimeoutError), span("GET api.github.com", kind="client"):
            raise TimeoutError
    finally:
        install_tracing(None)
    [point] = points(red)[CLIENT]
    assert as_dict(point["attributes"])["error.type"] == {"stringValue": "TimeoutError"}
    assert red.force_flush() is True
    red.shutdown()


# --- the pump and start-up -------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_pump_sends_logs_each_tick_and_metrics_each_minute() -> None:
    sender, logs, red, clock = FakeSender(), LogQueue(), RedMetrics(0), Clock()
    pump = _Pump(sender, logs, red, RESOURCE, clock=clock)
    red.on_end(finished("work", 0.01))
    logs({"event": "one"})
    pump.tick()
    assert len(sender.logs) == 1
    assert sender.metrics == []
    clock.now = METRICS_EVERY_S - 0.1
    pump.tick()
    assert sender.metrics == []
    assert len(sender.logs) == 1  # nothing new queued: nothing sent
    clock.now = METRICS_EVERY_S
    pump.tick()
    assert len(sender.metrics) == 1
    clock.now = METRICS_EVERY_S * 1.5
    pump.tick()
    assert len(sender.metrics) == 1  # the minute restarts from the last send


def test_a_long_queue_goes_in_batches() -> None:
    sender, logs = FakeSender(), LogQueue()
    for n in range(BATCH_SIZE + 1):
        logs({"event": f"e{n}"})
    _Pump(sender, logs, RedMetrics(0), RESOURCE).send_logs()
    assert len(sender.logs) == 2
    assert len(sender.log_records()) == BATCH_SIZE + 1


def test_no_metrics_body_before_any_span() -> None:
    sender = FakeSender()
    _Pump(sender, LogQueue(), RedMetrics(0), RESOURCE).send_metrics()
    assert sender.metrics == []


def test_the_pump_thread_runs_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing_module, "LOGS_EVERY_S", 0.01)
    sent = threading.Event()

    class Signalling(FakeSender):
        def send_logs(self, body: bytes) -> bool:
            sent.set()
            return super().send_logs(body)

    logs = LogQueue()
    pump = _Pump(Signalling(), logs, RedMetrics(0), RESOURCE)
    logs({"event": "e"})
    pump.start()
    assert sent.wait(5)
    pump.stop()
    pump.stop()  # twice is fine


def test_a_log_line_inside_a_span_ships_with_its_trace() -> None:
    sender = FakeSender()
    tracing = start_tracing(sender, version="1.2.3")
    with span("request", kind="server") as current:
        get_logger("t", sink=io.StringIO()).warning("revenue_sync_failed", reason="net.timeout", path="/x")
        ids = current.get_span_context()
    get_logger("yaqzan.net.otlp", sink=io.StringIO()).warning("telemetry_dropped", reason="net.auth")
    tracing.shutdown()
    get_logger("t", sink=io.StringIO()).info("after_shutdown")
    [record] = sender.log_records()
    assert record["body"] == {"stringValue": "revenue_sync_failed"}
    assert record["traceId"] == f"{ids.trace_id:032x}"
    assert record["spanId"] == f"{ids.span_id:016x}"
    assert record["severityText"] == "WARNING"
    assert "path" not in as_dict(record["attributes"])
    [metrics] = sender.metrics
    names = {m["name"] for m in json.loads(metrics)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert names == {SERVER}
    assert sender.order[-1] == "close"  # closed only after the last logs and metrics
    assert len(sender.traces) == 1


def test_counts_never_split_a_series() -> None:
    red = RedMetrics(0)
    for count in (1, 2, 3):
        attrs = {"server.address": "api.paddle.com", "yaqzan.count": count, "yaqzan.attempts": count}
        red.on_end(finished("GET api.paddle.com", 0.01, kind=SpanKind.CLIENT, attributes=attrs))
    [point] = points(red)[CLIENT]
    assert point["count"] == "3"


def test_nothing_is_queued_after_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[object] = []

    class Watched(LogQueue):
        def __call__(self, line: Any) -> None:
            queued.append(line.get("event"))
            super().__call__(line)

    monkeypatch.setattr(tracing_module, "LogQueue", Watched)
    tracing = start_tracing(FakeSender(), version="1")
    get_logger("t", sink=io.StringIO()).info("while_on")
    tracing.shutdown()
    get_logger("t", sink=io.StringIO()).info("after_off")
    assert queued == ["while_on"]
