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
(:func:`~agentzaaheen.core.sdk.explain.explain`), so a failure reads the same in the
logs, on the dashboard and in alerts; ``{said}`` in a sentence is that
catalogue's own one-line summary ("Paddle refused our API key.").

A test reads every ``say(...)`` call in the code and fails if an event is not
listed here, a sentence is missing, or a template names an unsafe field.
"""

from __future__ import annotations

import string
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agentzaaheen.core.sdk.explain import explain

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
    "chat_tool_refused": LogEvent(
        "warning",
        what="The chat's model asked for a tool or a choice that is not on, so it was refused ({reason}).",
        why="Only switched-on tools, with their listed choices, may run.",
        next=(
            "Nothing to do unless it keeps happening; then the model may be confused,"
            " or a question may be misusing tools."
        ),
    ),
    "chat_tool_failed": LogEvent(
        "warning",
        what="The chat tried to read {tool}, but the read failed.",
        explains="code",
    ),
    "agents_decided": LogEvent(
        "info",
        what="An agent change ({action}) by {actor} ended as {verdict}.",
        why="Someone gave a workspace an agent, or changed an agent's or a workspace's switch or cap.",
    ),
    "workspace_lifecycle_decided": LogEvent(
        "info",
        what="A project change ({action}) by {actor} ended as {verdict}.",
        why="Someone added a project, archived one, brought one back, or deleted one for good.",
    ),
    "agent_run_refused": LogEvent(
        "info",
        what="Agent {agent_id} was not allowed to run ({reason}).",
        why="Its switch, its workspace's switch or a spending cap said no; no cap means no spending.",
        next="Nothing to do unless it should be running: check its switch and caps on the workspace page.",
    ),
    "board_inspection": LogEvent(
        "info",
        what="The Board's chat looked inside the {workspace} workspace with {tool}.",
        why="The Chairman asked the Board a question that needed that workspace's own data "
        "(Board inspection).",
    ),
    "board_inspection_refused": LogEvent(
        "warning",
        what="The Board's chat did not look inside the {workspace} workspace with {tool}.",
        why="The look could not be written to the audit log first, so it was not made.",
        next="Check the laptop database is running (uv run python -m tools.laptop_db status).",
    ),
    "chat_switches_unreadable": LogEvent(
        "warning",
        what="The chat answered without any tools or skills.",
        why="The switches that say which tools are on could not be read ({code}), so everything stays off.",
        next="Check the laptop database is running (uv run python -m tools.laptop_db status).",
    ),
    # ---- the dashboard's API (one line per request: the route template, never the filled-in path)
    "api_answered": LogEvent(
        "info",
        what="The dashboard asked for {method} {route} and got http {http_status} in {duration_ms} ms.",
        why="The dashboard page on this laptop asked for it.",
    ),
    "workspace_api_answered": LogEvent(
        "info",
        what="The dashboard asked for {method} {route} in the {workspace} workspace "
        "and got http {http_status} in {duration_ms} ms.",
        why="The dashboard page on this laptop asked for it; the address chose the workspace.",
    ),
    "workspace_api_failed": LogEvent(
        "error",
        what="The dashboard's request {method} {route} in the {workspace} workspace "
        "failed (http {http_status}) after {duration_ms} ms.",
        why="Something inside Agent Zaaheen broke while answering it.",
        next="Open this request's trace (the trace_id on this line) to see which step failed.",
    ),
    "api_failed": LogEvent(
        "error",
        what="The dashboard's request {method} {route} failed (http {http_status}) after {duration_ms} ms.",
        why="Something inside Agent Zaaheen broke while answering it.",
        next="Open this request's trace (the trace_id on this line) to see which step failed.",
    ),
    # ---- outbound calls
    "net_answered": LogEvent(
        "info",
        what="A call to {host} was answered after {attempt} attempt(s) in {duration_ms} ms.",
        why="A connector read data it is allowed to read.",
    ),
    "net_failed": LogEvent(
        "warning",
        what="A call to {host} failed after {attempt} attempt(s) and {duration_ms} ms (http {http_status}).",
        explains="code",
    ),
    "probe_answered": LogEvent(
        "info",
        what="The uptime check of {host} got http {http_status} in {duration_ms} ms.",
        why="The uptime check asks each listed address whether it is up; the page itself is never read.",
    ),
    "probe_failed": LogEvent(
        "warning",
        what="The uptime check of {host} got no usable answer.",
        explains="code",
    ),
    "model_answered": LogEvent(
        "info",
        what="The local model answered in {duration_ms} ms.",
        why="The dashboard chat asked it a question.",
    ),
    "push_failed": LogEvent(
        "warning",
        what="A phone alert could not be sent (http {http_status}).",
        explains="code",
    ),
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
    "claude_answered": LogEvent(
        "info",
        what="Claude ({model}) answered in {duration_ms} ms.",
        why="A role's call was cleared by the model gate and sent through the one door to Claude.",
    ),
    "claude_call_failed": LogEvent(
        "warning",
        what="Claude ({model}) did not answer (http {status}): {said}",
        explains="code",
        vendor="source",
    ),
    "telemetry_dropped": LogEvent(
        "warning",
        what="Some {kind} for the log store were dropped and not sent.",
        explains="reason",
    ),
    "log_file_full": LogEvent(
        "warning",
        what="Today's log file is full ({bytes} bytes), so the rest of today's lines are not kept in it.",
        why="Far more log lines than usual were written today.",
        next="Look at the lines just before this one for what kept repeating; a new file starts tomorrow.",
    ),
    "push_sent": LogEvent(
        "info",
        what="A phone alert was sent ({bytes} bytes, priority {priority}).",
        why="Something changed that the Chairman asked to hear about.",
    ),
    "heartbeat_sent": LogEvent(
        "info",
        what="Told Healthchecks the {source} job ran.",
        why="Healthchecks alerts the Chairman's phone if this ping stops arriving.",
    ),
    "heartbeat_failed": LogEvent(
        "warning",
        what="Could not tell Healthchecks the {source} job ran (http {http_status}).",
        explains="code",
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
        why="The kill switches could not be read ({reason_code}); everything stops rather than guess.",
        next="Check the database is running; actions resume by themselves once the switches can be read.",
    ),
    "kill_switch_row_invalid": LogEvent(
        "error",
        what="Every agent action was stopped.",
        why="A stored kill switch makes no sense ({reason_code}); everything stops rather than guess.",
        next="Report it to Claude: the stored switch row needs to be checked and fixed.",
    ),
    # ---- the Customize page's switches and approvals (each also in the audit log)
    "registry_decided": LogEvent(
        "info",
        what="A Customize page change ({action}) by {actor} ended as {verdict}.",
        why="Someone used the Customize page; the same decision is kept in the audit log.",
    ),
    "passkey_decided": LogEvent(
        "info",
        what="A passkey step ({action}) ended as {verdict}.",
        why="The Chairman used Windows Hello on the Customize page; the audit log keeps the same decision.",
    ),
    "claude_key_decided": LogEvent(
        "info",
        what="Saving the Claude API key ended as {verdict}.",
        why="The Chairman saved the Claude key on the Customize page; the audit log keeps the same decision.",
    ),
    # ---- the database (one line per write: what kind of record, never its contents)
    "store_saved": LogEvent(
        "info",
        what="Wrote {record} to the database in {duration_ms} ms.",
        why="Agent Zaaheen keeps it so it is not forgotten.",
    ),
    "store_unchanged": LogEvent(
        "info",
        what="Nothing needed writing for {record} ({duration_ms} ms).",
        why="The database already had it, or it was no longer waiting, so nothing changed.",
    ),
    "store_write_failed": LogEvent(
        "warning",
        what="Could not save {record} to the database.",
        explains="code",
    ),
    # ---- local settings files read at start
    "settings_loaded": LogEvent(
        "info",
        what="The {setting} list was loaded ({entries} projects).",
        why="The dashboard was started with it.",
    ),
    "settings_refused": LogEvent(
        "error",
        what="The {setting} list was not used, so the dashboard did not start.",
        why="The file breaks one of its rules; the exact rule is printed on the dashboard's console.",
        next="Fix the file in projects/ and start the dashboard again.",
    ),
    "registry_item_refused": LogEvent(
        "warning",
        what="A {kind} was not loaded into the registry.",
        why="It broke a registry rule ({code}).",
        next="Fix the item's files, or remove it; everything else loaded normally.",
    ),
    # ---- what a workspace is built with (languages and providers, F17 slice 5b-1)
    "profile_read": LogEvent(
        "info",
        what="Read what the {workspace} workspace is built with: {entries} providers found.",
        why="Its repository changed since the last look, or this was the first look since the start.",
    ),
    "profile_failed": LogEvent(
        "warning",
        what="Could not read what the {workspace} workspace is built with: {said}",
        explains="code",
        vendor="source",
    ),
    # ---- the model map (F10, ADR-033): which model a role may use, customer data, spend caps
    "model_call_refused": LogEvent(
        "warning",
        what="The {role} role asked for a level {model_level} model and was refused ({reason}).",
        why="A role may only use a model at or below its pack's level ({role_level}); level 0 has no model.",
        next="Nothing is sent anywhere. If the role needs a stronger model, its pack must say so first.",
    ),
    "model_call_cleared_local": LogEvent(
        "info",
        what="The {role} role may use our own model {model} (level {model_level}).",
        why="It is at or below the role's level, and our own models cost nothing per call.",
    ),
    "model_customer_data_refused": LogEvent(
        "warning",
        what="A call to {model} from the {role} role was refused: the prompt holds customer data ({reason}).",
        why="Customer data never goes to a cloud model (ADR-023); the call is refused, not trimmed.",
        next="Run that job on our local model, or build its prompt from cleaned totals and findings only.",
    ),
    "model_cloud_check_passed": LogEvent(
        "info",
        what="The prompt for {model} passed the customer-data check ({checked} parts).",
        why=(
            "No part is tagged customer data, and the second net found no email, phone number,"
            " card number or customer ID."
        ),
    ),
    "model_call_cleared": LogEvent(
        "info",
        what=(
            "The {role} role may call {model} for {use}: up to {estimate_usd} USD,"
            " with {spent_usd} of {cap_usd} USD spent this month."
        ),
        why="The worst case of this call still fits under that use's cap and the monthly total.",
    ),
    "model_spend_refused": LogEvent(
        "warning",
        what=(
            "A call to {model} from the {role} role was refused ({reason}): up to {estimate_usd} USD"
            " would pass the {use} cap, with {spent_usd} of {cap_usd} USD spent this month."
        ),
        why="The monthly caps are in code (ADR-033) so no one use can eat the month's Claude credit.",
        next=(
            "The run is marked did not run, never answered by a weaker model."
            " Changing a cap is the Chairman's call."
        ),
    ),
    "model_spend_recorded": LogEvent(
        "info",
        what=(
            "A call to {model} cost {cost_usd} USD for {use}; {spent_usd} of {cap_usd} USD spent this month."
        ),
        why="Worked out from the API's own token counts and the pinned price table.",
    ),
    "model_spend_notice": LogEvent(
        "warning",
        what="The {use} cap is at {percent}% of its {cap_usd} USD this month.",
        why="The last Claude call crossed the 80% line.",
        next="It is named in the morning briefing; at 100% that use stops until next month.",
    ),
    "model_names_unreadable": LogEvent(
        "warning",
        what="Our customers' names could not be read for the customer-data check, so the cloud call stopped.",
        why="Without the name list a customer's name could slip through, so no cloud call may go ({reason}).",
        next="Check the database is up; the call can be asked again once the names can be read.",
    ),
    "model_cloud_call_failed": LogEvent(
        "warning",
        what="The {role} role's call to {model} for {use} (run {run_id}) did not get an answer: {said}",
        explains="code",
    ),
    "model_cloud_call_done": LogEvent(
        "info",
        what=(
            "The {role} role's call to {model} for {use} (run {run_id}) finished ({stop}):"
            " {tokens_in} tokens in, {tokens_out} out, {cache_read} read from cache,"
            " {cache_write} written to cache; it cost {cost_usd} USD."
        ),
        why="Every Claude call is logged with its counts and cost, never its text (ADR-033).",
    ),
    # ---- the scout (F17 slice 5b-2): it reads a repository and advises; it switches nothing on
    "scout_asked": LogEvent(
        "info",
        what="The scout was asked about the {workspace} workspace: {verdict}.",
        why="The Chairman pressed Ask the scout on that workspace's page.",
    ),
    "scout_finished": LogEvent(
        "info",
        what="The scout finished the {workspace} workspace in {duration_ms} ms: {entries} roles reviewed.",
        why="It read the repository, the local model answered, and code checked the answer.",
    ),
    "scout_failed": LogEvent(
        "warning",
        what="The scout could not finish the {workspace} workspace (stage {stage}): {said}",
        explains="code",
    ),
    # ---- daily summaries and the morning briefing (F17 slice 6): written by code, read-only
    "summary_saved": LogEvent(
        "info",
        what="Saved today's summary for the {workspace} workspace.",
        why="The morning job ran, or the Chairman asked for today's summaries again.",
    ),
    "summary_failed": LogEvent(
        "warning",
        what="Could not make today's summary for the {workspace} workspace: {said}",
        explains="code",
    ),
    "summaries_made": LogEvent(
        "info",
        what="Made {entries} workspace summaries for today ({failing} could not be made).",
        why="It was past the gathering time in the Chairman's time zone, or he asked for them again.",
    ),
    "briefing_sent": LogEvent(
        "info",
        what="The Board's morning briefing was sent to the Chairman's phone.",
        why="It was past the briefing time in the Chairman's time zone and today's had not gone yet.",
    ),
    "briefing_not_sent": LogEvent(
        "warning",
        what="The Board's morning briefing could not be sent: {said}",
        explains="code",
    ),
    "briefing_job_failed": LogEvent(
        "warning",
        what="The morning summaries job could not finish this check.",
        why="It stopped with an unexpected {kind} error.",
        next=f"It checks again in a minute. {_SEE_DASHBOARD}",
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
    "uptime_round_done": LogEvent(
        "info",
        what="An uptime round checked {checked} addresses: {failing} not up, {changes} changes to report.",
        why="The uptime check runs every 5 minutes.",
    ),
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


def elapsed_ms(started: float) -> int:
    """Whole milliseconds since ``started`` (a ``time.monotonic()`` reading), for a line's ``duration_ms``."""
    return max(0, round((time.monotonic() - started) * 1000))


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
