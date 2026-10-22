"""Every log event in plain English: what happened, why, and what to do next (ADR-034).

The Chairman's rule (22 Oct 2026): a log line must say **what happened and why**,
not only name an event, so he, Claude and the agents can all read it. So every
log call goes through :func:`say` with an event code from :data:`LOG_EVENTS`, and
each line carries:

- ``event``: the short code machines filter on (``uptime_round_failed``);
- ``what``: one sentence, what happened;
- ``why``: one sentence, the cause (or that it is not known);
- ``next``: what to check or do (always for warnings and errors).

Blanks such as ``{project}`` are filled **only with our own values**: codes,
counts, durations, names we chose. Templates may only name fields the log
store is allowed to receive (``telemetry.logs.LOG_FIELDS``), so a sentence can
never carry text an outsider wrote. When an event carries a reason code
(``explains``), its ``why`` and ``next`` come from the error catalogue
(:func:`~yaqzan.core.sdk.explain.explain`), so a failure reads the same in the
logs, on the dashboard and in alerts; ``{said}`` in a sentence is that
catalogue's own one-line summary ("Paddle refused our API key.").

A test reads every ``say(...)`` call in the code and fails if an event is not
listed here, a sentence is missing, or a template names an unsafe field.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from yaqzan.core.sdk.explain import explain

type Level = Literal["info", "warning", "error"]

UNKNOWN = "unknown"
# In a sentence of an event that ``explains`` a code: the catalogue's summary of it.
SAID = "said"
_NO_CAUSE = "The cause is not known."


@dataclass(frozen=True)
class LogEvent:
    """One event's sentences. Empty ``why``/``next`` are taken from the code in ``explains``."""

    level: Level
    what: str
    why: str = ""
    next: str = ""
    # The field holding a reason code (``code``, ``reason``): its catalogue text fills ``why`` and ``next``.
    explains: str | None = None
    # The field naming the vendor for that text ("Paddle" instead of "the service").
    vendor: str | None = None


_SEE_DASHBOARD = "Nothing to do unless it keeps happening; the dashboard shows the latest state."

LOG_EVENTS: Mapping[str, LogEvent] = {
    # ---- the dashboard chat
    "chat_failed": LogEvent(
        "warning",
        what="A question asked in the dashboard chat could not be answered (http {status}).",
        explains="code",
    ),
    "registry_store_failed": LogEvent(
        "warning",
        what="A Customize page request could not be completed (the page answered 503).",
        explains="code",
    ),
    "chat_tool_used": LogEvent(
        "info",
        what="The chat read {tool} to answer a question ({chars} characters).",
        why="The model asked for it, and the tool is switched on.",
    ),
    "chat_tool_failed": LogEvent(
        "warning",
        what="The chat tried to read {tool}, but the read failed.",
        explains="code",
    ),
    "chat_switches_unreadable": LogEvent(
        "warning",
        what="The chat answered without any tools or skills.",
        why="The switches that say which tools are on could not be read ({code}), so everything stays off.",
        next="Check the laptop database is running (uv run python -m tools.laptop_db status).",
    ),
    # ---- outbound calls
    "net_download": LogEvent(
        "info",
        what="Downloaded {bytes} bytes from {download_host}.",
        why="A connector asked for a file it is allowed to fetch.",
    ),
    "net_retry": LogEvent(
        "warning",
        what="An outside call failed and will be tried again (attempt {attempt}) in {delay_s} seconds.",
        explains="reason",
    ),
    "model_call_failed": LogEvent(
        "warning",
        what="The local model did not answer (http {status}).",
        explains="code",
    ),
    "telemetry_dropped": LogEvent(
        "warning",
        what="Some {kind} for the log store were dropped and not sent.",
        explains="reason",
    ),
    "push_sent": LogEvent(
        "info",
        what="A phone alert was sent ({bytes} bytes, priority {priority}).",
        why="Something changed that the Chairman asked to hear about.",
    ),
    # ---- alerts
    "alert_sent": LogEvent(
        "info",
        what="The phone alert for {project} was sent (down: {down}).",
        why="Its addresses changed state.",
    ),
    "alert_not_sent": LogEvent(
        "warning",
        what="The phone alert for {project} could not be sent (down: {down}).",
        explains="code",
    ),
    # ---- safety switches
    "kill_switches_unreadable": LogEvent(
        "error",
        what="Every agent action was stopped.",
        why="The kill switches could not be read ({reason_code}); Yaqzan stops everything rather than guess.",
        next="Check the database is running; actions resume by themselves once the switches can be read.",
    ),
    "kill_switch_row_invalid": LogEvent(
        "error",
        what="Every agent action was stopped.",
        why="A stored kill switch makes no sense ({reason_code}); Yaqzan stops everything rather than guess.",
        next="Report it to Claude: the stored switch row needs to be checked and fixed.",
    ),
    "registry_item_refused": LogEvent(
        "warning",
        what="A {kind} was not loaded into the registry.",
        why="It broke a registry rule ({code}).",
        next="Fix the item's files, or remove it; everything else loaded normally.",
    ),
    # ---- revenue sync
    "revenue_synced": LogEvent(
        "info",
        what="Revenue was synced: {subscriptions} subscriptions, {payments} payments, {refunds} refunds.",
        why="The scheduled sync ran.",
    ),
    "revenue_sync_failed": LogEvent(
        "warning",
        what="The revenue sync stopped while trying to {stage}: {said}",
        explains="reason_code",
        vendor="source",
    ),
    "revenue_sync_crashed": LogEvent(
        "error",
        what="The revenue sync from {source} crashed while trying to {stage}.",
        explains="reason_code",
        vendor="source",
    ),
    # ---- uptime
    "uptime_round_failed": LogEvent(
        "warning",
        what="One round of uptime re-checks could not run.",
        why="It stopped with an unexpected {kind} error.",
        next=f"The next round runs in 5 minutes. {_SEE_DASHBOARD}",
    ),
}


def say(log: Any, name: str, /, **fields: object) -> None:  # noqa: ANN401  # structlog's bound logger
    """Write one log line for event ``name``, with its sentences and the fields machines read.

    ``log`` and ``name`` are positional only, so an event may carry a field called ``code``.
    """
    event = LOG_EVENTS.get(name)
    if event is None:  # a test keeps this from happening; still never lose the line
        log.warning(
            name, what=f"Something happened that has no description yet ({name}).", why=_NO_CAUSE, next=""
        )
        return
    said = None
    if event.explains is not None:
        said = explain(str(fields.get(event.explains) or UNKNOWN), vendor=_vendor(event, fields))
        fields = {**fields, SAID: said.what}
    what = _fill(event.what, fields)
    why, after = _fill(event.why, fields), _fill(event.next, fields)
    if said is not None:
        why, after = why or said.why, after or said.fix
    getattr(log, event.level)(name, what=what, why=why or _NO_CAUSE, next=after, **fields)


def placeholders(template: str) -> set[str]:
    """The field names a template fills in (``{project}`` → ``project``)."""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def _fill(template: str, fields: Mapping[str, object]) -> str:
    return template.format_map({name: _word(fields.get(name)) for name in placeholders(template)})


def _word(value: object) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _vendor(event: LogEvent, fields: Mapping[str, object]) -> str | None:
    value = fields.get(event.vendor) if event.vendor is not None else None
    return str(value) if value is not None else None
