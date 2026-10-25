"""Log lines for the log store: an allow-listed part of each redacted line, sent in batches (ADR-032).

The logger hands every finished line to :class:`LogQueue` (through
``core.sdk.install_log_sink``). Only the fields in :data:`LOG_FIELDS` leave, each
a plain number, flag or short redacted string; free text, paths, tool arguments,
tenant IDs and tracebacks stay on the laptop. Each line's plain-English ``what``
becomes the body (ADR-034; the event name when a line has none), ``why`` and
``next`` travel as attributes with the ``event`` code, the level becomes its
severity, and ``trace_id``/``span_id`` join the line to its trace. The sentences
are safe to send: they are built only from our own templates and these fields
(``core.sdk.events``).

The queue is bounded: when it is full, new lines are dropped and counted.
The sender's own ``telemetry_dropped`` lines are never queued, so a store that
is down cannot fill the queue with reports about itself.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Mapping

from yaqzan.core.telemetry.encode import Json, attributes, resource, text

QUEUE_SIZE = 2_000
BATCH_SIZE = 500
SKIPPED_EVENTS = frozenset({"telemetry_dropped"})

# Codes, names, counts and durations, and the plain-English sentences built from them (ADR-034).
LOG_FIELDS: frozenset[str] = frozenset(
    {
        "component",
        "event",
        "what",
        "why",
        "next",
        "down",
        "code",
        "reason",
        "reason_code",
        "status",
        "http_status",
        "stage",
        "project",
        "project_id",
        "workspace",
        "agent_id",
        "source",
        "kind",
        "tool",
        "effect",
        "priority",
        "model",
        "host",
        "download_host",
        "attempt",
        "delay_s",
        "bytes",
        "chars",
        "payments",
        "refunds",
        "subscriptions",
        # ADR-034 slice 2: the doors (our own route templates, record kinds, roles and counts)
        "method",
        "route",
        "duration_ms",
        "record",
        "action",
        "verdict",
        "actor",
        "setting",
        "entries",
        "checked",
        "failing",
        "changes",
        # F10: the model map (our own role ids, levels, use names and amounts)
        "role",
        "model_level",
        "role_level",
        "use",
        "percent",
        "estimate_usd",
        "spent_usd",
        "cap_usd",
        "cost_usd",
        "run_id",
        "tokens_in",
        "tokens_out",
        "cache_read",
        "cache_write",
        "stop",
    }
)

_SEVERITY = {"debug": 5, "info": 9, "warning": 13, "error": 17, "critical": 21}
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


class LogQueue:
    """Holds log records until the next send. Safe to use from any thread."""

    def __init__(self, size: int = QUEUE_SIZE) -> None:
        self._records: deque[Json] = deque()
        self._size = size
        self._lock = threading.Lock()
        self._dropped = 0

    def __call__(self, line: Mapping[str, object]) -> None:
        """Queue one redacted log line (the logger's sink)."""
        if line.get("event") in SKIPPED_EVENTS:
            return
        record = encode_record(line, time.time_ns())
        with self._lock:
            if len(self._records) >= self._size:
                self._dropped += 1
                return
            self._records.append(record)

    def take(self, limit: int = BATCH_SIZE) -> list[Json]:
        """Remove and return up to ``limit`` records, oldest first."""
        with self._lock:
            return [self._records.popleft() for _ in range(min(limit, len(self._records)))]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def dropped(self) -> int:
        """Lines dropped because the queue was full."""
        with self._lock:
            return self._dropped


def encode_record(line: Mapping[str, object], now_ns: int) -> Json:
    """One OTLP log record from a redacted log line; only :data:`LOG_FIELDS` are kept."""
    level = str(line.get("level", "info"))
    record: Json = {
        "timeUnixNano": str(now_ns),
        "severityNumber": _SEVERITY.get(level, 9),
        "severityText": text(level.upper()),
        "body": {"stringValue": text(str(line.get("what") or line.get("event", "")))},
        "attributes": attributes(line, LOG_FIELDS),
    }
    trace_id, span_id = str(line.get("trace_id", "")), str(line.get("span_id", ""))
    if _TRACE_ID.fullmatch(trace_id) and _SPAN_ID.fullmatch(span_id):
        record["traceId"], record["spanId"] = trace_id, span_id
    return record


def encode_logs(records: list[Json], resource_values: Mapping[str, object]) -> Json:
    """One OTLP/JSON logs request body."""
    if not records:
        return {"resourceLogs": []}
    return {
        "resourceLogs": [
            {
                "resource": resource(resource_values),
                "scopeLogs": [{"scope": {"name": "yaqzan", "version": ""}, "logRecords": records}],
            }
        ]
    }
