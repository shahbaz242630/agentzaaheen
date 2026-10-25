"""Supabase JSON to the SDK's ``BackendHealth``: codes and numbers only.

Only these fields are ever read: a project's ``status``; each health entry's
``name`` and ``healthy``; disk utilisation's ``metrics.fs_size_bytes`` and
``metrics.fs_used_bytes``; usage's ``result[].total_*_requests``. Error
messages, descriptions, names and anything else the vendor sends are never
touched (whoever controls a project can write them).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from agentzaaheen.core.sdk import ProjectState, ServiceHealth, SourceError, SourceErrorCode

SOURCE = "supabase"
Json = Mapping[str, object]

_STATES: Mapping[str, ProjectState] = {
    "ACTIVE_HEALTHY": ProjectState.HEALTHY,
    "ACTIVE_UNHEALTHY": ProjectState.UNHEALTHY,
    "COMING_UP": ProjectState.STARTING,
    "RESTORING": ProjectState.STARTING,
    "RESTARTING": ProjectState.STARTING,
    "UPGRADING": ProjectState.STARTING,
    "RESIZING": ProjectState.STARTING,
    "INACTIVE": ProjectState.PAUSED,
    "PAUSING": ProjectState.PAUSED,
    "GOING_DOWN": ProjectState.PAUSED,
    "INIT_FAILED": ProjectState.FAILED,
    "RESTORE_FAILED": ProjectState.FAILED,
    "PAUSE_FAILED": ProjectState.FAILED,
    "REMOVED": ProjectState.REMOVED,
}
# The services we ask about; a name outside this list is ignored, never shown.
SERVICES = ("db", "auth", "rest", "storage", "realtime")
_USAGE_FIELDS = (
    "total_rest_requests",
    "total_auth_requests",
    "total_storage_requests",
    "total_realtime_requests",
)
_MAX_COUNT = 10**12
_NAME = re.compile(r"[a-z_]{1,32}")


def _bad(what: str) -> SourceError:
    return SourceError(SourceErrorCode.BAD_RESPONSE, f"supabase sent an unusable {what}")


def state(project: Json) -> ProjectState:
    """The project's state; a status we do not know reads as ``unknown`` (never as healthy)."""
    status = project.get("status")
    if not isinstance(status, str):
        raise _bad("project status")
    return _STATES.get(status, ProjectState.UNKNOWN)


def services(entries: Sequence[object], wanted: Sequence[str] = SERVICES) -> tuple[ServiceHealth, ...]:
    """Each ``wanted`` service and whether it is healthy, in that order; any other name is skipped."""
    found: dict[str, bool] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _bad("health entry")
        name, healthy = entry.get("name"), entry.get("healthy")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(healthy, bool):
            raise _bad("health entry")
        found[name] = healthy
    return tuple(ServiceHealth(name=n, healthy=found[n]) for n in wanted if n in found)


def _whole(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _bad(what)
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > _MAX_COUNT * 10**3:  # NaN, negative or absurd
        raise _bad(what)
    return int(number)


def disk(util: Json) -> tuple[int, int]:
    """(used bytes, size bytes) from disk utilisation."""
    metrics = util.get("metrics")
    if not isinstance(metrics, Mapping):
        raise _bad("disk utilisation")
    size = _whole(metrics.get("fs_size_bytes"), "disk size")
    used = _whole(metrics.get("fs_used_bytes"), "disk used")
    if size <= 0 or used > size:
        raise _bad("disk utilisation")
    return used, size


def requests(usage: Json) -> int:
    """All API requests in the answer's window (REST, auth, storage, realtime)."""
    if "error" in usage and usage.get("error"):
        raise SourceError(SourceErrorCode.UNAVAILABLE, "supabase could not give usage counts")
    rows = usage.get("result", [])
    if not isinstance(rows, list):
        raise _bad("usage counts")
    total = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise _bad("usage row")
        total += sum(_whole(row.get(field, 0), "usage count") for field in _USAGE_FIELDS)
    if total > _MAX_COUNT:
        raise _bad("usage counts")
    return total
