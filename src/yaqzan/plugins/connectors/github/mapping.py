"""GitHub JSON to the SDK's ``RepoHealth``: counts, one commit id and times, nothing written by people.

Only these fields are ever read: a repository's ``default_branch``, a search's
``total_count`` and ``incomplete_results``, and each workflow run's
``workflow_id``, ``head_sha``, ``status``, ``conclusion``, ``created_at`` and
``updated_at``. Titles, bodies, names and commit messages are never touched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from yaqzan.core.sdk import BuildState, SourceError, SourceErrorCode

SOURCE = "github"

Json = Mapping[str, object]

# A workflow run that has not finished yet (GitHub's run ``status`` values).
_UNFINISHED = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
# How a finished run ended: these count as a broken build ...
_FAILING = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
# ... and these do not (a cancelled or skipped run says nothing bad about the code).
_NOT_FAILING = frozenset({"success", "neutral", "skipped", "cancelled", "stale"})
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,255}")
_SHA_LENGTH = 40
_HEX = frozenset("0123456789abcdef")


def _bad(what: str) -> SourceError:
    return SourceError(SourceErrorCode.BAD_RESPONSE, f"github sent an unusable {what}")


def obj(value: object, what: str) -> Json:
    """``value`` as a JSON object, or raise."""
    if not isinstance(value, Mapping):
        raise _bad(what)
    return value


def objects(value: object, what: str) -> list[Json]:
    """``value`` as a list of JSON objects, or raise."""
    if not isinstance(value, list):
        raise _bad(what)
    return [obj(item, what) for item in value]


def text(data: Json, key: str) -> str:
    """A required, non-empty string field."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise _bad(key)
    return value


def count(data: Json, key: str) -> int:
    """A required whole number, zero or more (``True`` is not a number here)."""
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _bad(key)
    return value


def time(data: Json, key: str) -> datetime:
    """A required ISO 8601 time with a timezone."""
    try:
        value = datetime.fromisoformat(text(data, key))
    except ValueError:
        raise _bad(key) from None
    if value.tzinfo is None:
        raise _bad(key)
    return value


def commit(data: Json, key: str) -> str:
    """A full 40-character commit id."""
    value = text(data, key)
    if len(value) != _SHA_LENGTH or not set(value) <= _HEX:
        raise _bad(key)
    return value


def branch(data: Json, key: str) -> str:
    """A branch name made only of the characters git refs are written with here (no spaces or ``;``)."""
    value = text(data, key)
    if not _BRANCH.fullmatch(value):
        raise _bad(key)
    return value


def search_total(page: Json) -> int:
    """The match count of one search. A count GitHub says is incomplete is refused, not shown."""
    if page.get("incomplete_results") is not False:
        raise SourceError(SourceErrorCode.UNKNOWN_VALUE, "github could not finish counting")
    return count(page, "total_count")


@dataclass(frozen=True)
class Build:
    """The newest commit's build, summed over its workflows (the result of :func:`build`)."""

    state: BuildState
    commit: str | None
    failing: int
    finished_at: datetime | None


def build(runs: Sequence[Json]) -> Build:
    """How the newest commit on the branch built: each workflow's latest run on that commit decides.

    One failed workflow fails the build, even while others still run. Otherwise
    any unfinished workflow means it is still running.
    """
    if not runs:
        return Build(BuildState.NONE, None, 0, None)
    newest = max(runs, key=lambda run: time(run, "created_at"))
    head = commit(newest, "head_sha")
    latest: dict[int, Json] = {}
    for run in runs:
        if commit(run, "head_sha") != head:
            continue
        workflow = count(run, "workflow_id")
        if workflow not in latest or time(run, "created_at") > time(latest[workflow], "created_at"):
            latest[workflow] = run
    failing = 0
    running = False
    finished: list[datetime] = []
    for run in latest.values():
        status = text(run, "status")
        if status in _UNFINISHED:
            running = True
            continue
        if status != "completed":
            raise SourceError(SourceErrorCode.UNKNOWN_VALUE, "github sent a run status we do not know")
        conclusion = text(run, "conclusion")
        if conclusion not in _FAILING | _NOT_FAILING:
            raise SourceError(SourceErrorCode.UNKNOWN_VALUE, "github sent a run conclusion we do not know")
        failing += conclusion in _FAILING
        finished.append(time(run, "updated_at"))
    if failing:
        return Build(BuildState.FAILED, head, failing, max(finished))
    if running:
        return Build(BuildState.RUNNING, head, 0, None)
    return Build(BuildState.PASSED, head, 0, max(finished))
