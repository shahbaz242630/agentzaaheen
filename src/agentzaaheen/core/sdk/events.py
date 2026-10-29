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
    "custom_mcp_store_failed": LogEvent(
        "warning",
        what="A request on the Customize page's MCP tab could not be completed (the page answered 503).",
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
    # ---- the runner (ADR-036, F11): every agent run starts and ends here
    "agent_run_not_started": LogEvent(
        "info",
        what="Agent {agent_id} was not started ({reason}).",
        why="It is switched off, paused, or a stop switch is on, so it was not meant to run.",
        next="Nothing to do unless it should be running: check its switches on the workspace page.",
    ),
    "agent_run_denied": LogEvent(
        "warning",
        what="The {role} agent ({agent_id}) did not run: {reason}.",
        why="One of the runner's checks said no before it started (a cap used up, a missing role copy, "
        "or tools it may not have).",
        next="The morning briefing names it. Check the agent's caps and role on the workspace page.",
    ),
    "agent_run_started": LogEvent(
        "info",
        what="The {role} agent started run {run_id}, with {seconds} seconds to finish.",
        why="Every check before the run said yes, and the start was written down first.",
    ),
    "agent_run_ended": LogEvent(
        "info",
        what="The {role} agent's run {run_id} ended: {outcome} ({reason}), {findings} finding(s), "
        "cost {cost_minor} cent(s), {bookmarks} bookmark(s) moved.",
        why="The run's end, its findings, its cost and where it got to were saved together.",
    ),
    "agent_findings_leak_checked": LogEvent(
        "warning",
        what="Before saving the {role} agent's run {run_id}: {held_back} finding(s) held back for holding a "
        "secret, {retagged} moved up to customer data.",
        why="Nothing leaves a run unchecked (F11, before anything leaves): a secret is never kept, and "
        "customer details never reach a cloud model.",
        next="A held-back finding means the agent copied a key or password: tell Claude which agent.",
    ),
    "leak_names_unreadable": LogEvent(
        "warning",
        what="The leak check could not read our customers' names, so names were not checked this run.",
        why="The name list could not be read; secrets and customer details were still checked.",
        next=f"Nothing to do unless it keeps happening. {_SEE_DASHBOARD}",
    ),
    "agent_run_crashed": LogEvent(
        "error",
        what="The {role} agent's code failed during its run ({kind}).",
        why="A bug in that agent's code; the runner ended the run as 'could not run'.",
        next="The morning briefing names it; look at the agent's code for this error.",
    ),
    "agent_run_end_not_saved": LogEvent(
        "error",
        what="The end of the {role} agent's run {run_id} could not be saved ({code}).",
        why="The database refused or could not be reached.",
        next="Nothing is lost: it is closed as interrupted after its deadline and named in the briefing.",
    ),
    "agent_answer_refused": LogEvent(
        "warning",
        what="The {role} agent's answer (try {attempt}) was refused by the checks ({code}) in run {run_id}.",
        why="Code checks every model answer against the role's shape; a wrong one goes back with the reason.",
        next="Nothing to do: it is asked again, at most twice; after that the run needs a look.",
    ),
    "agent_needs_a_look": LogEvent(
        "warning",
        what="The {role} agent's run {run_id} needs a look: {attempt} answers in a row were refused "
        "({code}).",
        why="The model kept answering in the wrong shape; nothing was guessed or filled in.",
        next="The morning briefing names it. Try again later, or check the model is the one expected.",
    ),
    "agent_runs_interrupted": LogEvent(
        "warning",
        what="Agent runs that never finished were closed as interrupted: {count} ({run_ids}).",
        why="A run started but never wrote its end: the laptop slept, the server restarted, "
        "or it stopped halfway.",
        next="The morning briefing names them; if it keeps happening, check the machine was awake.",
    ),
    "agent_runs_not_closed": LogEvent(
        "warning",
        what="Runs that never finished could not be checked ({code}).",
        why="The database refused or could not be reached.",
        next="Nothing to do unless it keeps happening; they are checked again before the next run.",
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
    "cert_answered": LogEvent(
        "info",
        what="The certificate of {host} was read ({reason}).",
        why="Monitoring checks when each site's certificate ends, every 6 hours.",
    ),
    "cert_failed": LogEvent(
        "warning",
        what="The certificate of {host} could not be read.",
        explains="code",
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
    "push_secret_refused": LogEvent(
        "warning",
        what="A phone message was not sent: it holds a key, token or password.",
        why="A secret never leaves Agent Zaaheen (F11, before anything leaves); it is refused, not trimmed.",
        next="Tell Claude: a phone message is built from our own words and numbers, so a secret means a bug.",
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
    "custom_mcp_decided": LogEvent(
        "info",
        what="A custom MCP server step ({action}) on {server} by {actor} ended as {verdict} "
        "(test result: {outcome}).",
        why="Someone used the MCP tab; nothing there can be used by an agent until approved (ADR-041). "
        "The same decision is kept in the audit log.",
    ),
    "mcp_tools_listed": LogEvent(
        "info",
        what="A custom MCP server listed {tools} tools ({skipped} left out for unusable names) "
        "in {duration_ms} ms.",
        why="The Chairman pressed Test: we asked only for its tool list, sent none of our keys or text, "
        "and called no tool.",
    ),
    "mcp_test_failed": LogEvent(
        "warning",
        what="A custom MCP server's test did not list its tools (http {status}) after {duration_ms} ms.",
        explains="code",
    ),
    "passkey_decided": LogEvent(
        "info",
        what="A passkey step ({action}) ended as {verdict}.",
        why="The Chairman used Windows Hello on the Customize page; the audit log keeps the same decision.",
    ),
    "session_started": LogEvent(
        "info",
        what="The Chairman signed in to the dashboard.",
        why="Windows Hello signed the sign-in challenge (or set up a new passkey); "
        "the audit log keeps the same.",
    ),
    "session_ended": LogEvent(
        "info",
        what="A dashboard sign-in ended ({reason}).",
        why="Sign-ins end on Sign out, after 3 hours with nothing done, after 12 hours at most, "
        "or when a 4th sign-in replaces the oldest.",
    ),
    "sign_in_failed": LogEvent(
        "warning",
        what="A sign-in to the dashboard failed.",
        why="The Windows Hello answer did not check out; the audit log keeps the reason.",
        next="If the Chairman did not just try to sign in, check what else is running on this laptop.",
    ),
    "sign_in_waiting": LogEvent(
        "warning",
        what="Sign-in to the dashboard now waits 1 minute.",
        why="Five sign-ins failed within 10 minutes.",
        next="If the Chairman did not make those tries, check what else is running on this laptop.",
    ),
    "session_audit_failed": LogEvent(
        "warning",
        what="A sign-out could not be written to the audit log ({code}).",
        why="The database did not take the record; the sign-out itself still happened.",
        next="Check the laptop database is running.",
    ),
    "claude_key_decided": LogEvent(
        "info",
        what="Saving the Claude API key ended as {verdict}.",
        why="The Chairman saved the Claude key on the Customize page; the audit log keeps the same decision.",
    ),
    "server_token_decided": LogEvent(
        "info",
        what="Saving the server access token ended as {verdict}.",
        why="The Chairman saved it on the Customize page; the audit log keeps the same decision.",
    ),
    "model_key_decided": LogEvent(
        "info",
        what="Saving our server model's key ended as {verdict}.",
        why="The Chairman saved it on the Customize page; the audit log keeps the same decision.",
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
        what="The {setting} list was loaded ({entries} entries).",
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
    "model_secret_refused": LogEvent(
        "warning",
        what="A call to {model} from the {role} role was refused: the prompt holds a key, token or password.",
        why="A secret never leaves Agent Zaaheen (F11, before anything leaves); it is refused, not trimmed.",
        next="Find where the secret entered the prompt and keep it out; tell Claude if it keeps happening.",
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
    "model_miss_not_recorded": LogEvent(
        "error",
        what=(
            "The {role} role's run {run_id} did not run, and that could not be written down,"
            " so the morning briefing will not name it: {said}"
        ),
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
    # ---- chat records (F19 slice 1): every chat kept; the words never go in a log line
    "chat_turn_kept": LogEvent(
        "info",
        what="Kept turn {entries} of the {workspace} chat ({stage}).",
        why="A question in the dashboard chat was answered, and every chat is kept as a record.",
    ),
    "chat_record_failed": LogEvent(
        "warning",
        what="A chat answer was shown but could not be kept as a record (the {workspace} chat): {said}",
        explains="code",
    ),
    "chats_forgotten": LogEvent(
        "info",
        what="Removed {entries} chat conversations not used for 12 months.",
        why="Chats are kept 12 months (Chairman, 27 Oct 2026); the daily clean-up ran.",
    ),
    "chats_forget_failed": LogEvent(
        "warning",
        what="Could not remove old chats from one workspace this time: {said}",
        explains="code",
        next="It is tried again tomorrow. If this repeats, check the laptop database is running.",
    ),
    "chats_sweep_failed": LogEvent(
        "warning",
        what="The daily clean-up of old chats could not run this time.",
        why="It stopped with an unexpected {kind} error.",
        next="It is tried again tomorrow. If this repeats, check the laptop database is running.",
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
    "reread_done": LogEvent(
        "info",
        what="Read {source} again: {failing} projects could not be read.",
        why="Paddle, GitHub and Supabase are read again every hour, so their cards never go out of date.",
    ),
    "reread_failed": LogEvent(
        "warning",
        what="Could not read {source} again, so its cards keep the last read: {said}",
        explains="code",
        vendor="source",
    ),
    "reread_crashed": LogEvent(
        "warning",
        what="Could not read {source} again, so its cards keep the last read.",
        why="It stopped with an unexpected {kind} error.",
        next="The next try is in an hour; the cards turn grey if the read gets too old.",
    ),
    "monitoring_agents_unreadable": LogEvent(
        "warning",
        what="Could not read {project}'s agents, so its Monitoring agent was not woken this round.",
        why="The database refused the read ({code}).",
        next=f"The next round is in 5 minutes. {_SEE_DASHBOARD}",
    ),
    "healthchecks_read_done": LogEvent(
        "info",
        what="Read {checks} Healthchecks checks for the Monitoring agents.",
        why="Monitoring reads Healthchecks in each 5-minute round that has an agent to wake.",
    ),
    "healthchecks_read_failed": LogEvent(
        "warning",
        what="Could not read Healthchecks, so Monitoring keeps the last read.",
        why="The read was refused or did not answer ({code}).",
        next="The next round tries again in 5 minutes; after 15 minutes without a read, "
        "Monitoring writes that Healthchecks could not be read. Healthchecks still alerts the phone itself.",
    ),
    "availability_read_done": LogEvent(
        "info",
        what="Read {count} hourly uptime counts from the external checker for the SLO numbers.",
        why="The SLO numbers are worked out from the checker's counts, read at most once an hour.",
    ),
    "availability_read_failed": LogEvent(
        "warning",
        what="Could not read the external checker's uptime counts, so the SLO numbers keep the last read.",
        why="The read was refused or did not answer ({code}).",
        next="It is tried again the next time the SLO numbers are asked for; after 2 hours without a read "
        "they are shown as unknown, never as fine.",
    ),
    "expiry_read_done": LogEvent(
        "info",
        what="Read the end dates of {checks} {kind} names for Monitoring ({unread} could not be read).",
        why="Monitoring reads certificate dates every 6 hours and domain dates once a day.",
    ),
    "expiry_read_failed": LogEvent(
        "warning",
        what="Could not read the {kind} end dates, so Monitoring keeps the last read.",
        why="It stopped with an unexpected {code} problem.",
        next="It tries again in the next 5-minute round; Monitoring says when the dates are too old.",
    ),
    # ---- the knowledge bank (ADR-037, F12): counts and names, never a document's words
    "knowledge_synced": LogEvent(
        "info",
        what="Read {workspace}'s product documents: {entries} listed files found, {changes} read again "
        "({count} passages kept), {removed} no longer listed removed.",
        why="The knowledge bank re-reads each project's listed documents at start and every 6 hours; "
        "a file is read again only when it changed.",
    ),
    "knowledge_missing": LogEvent(
        "warning",
        what="Some files in {workspace}'s knowledge list ({entries}) were not found on its main branch.",
        why="They were renamed, deleted, are not a kind of document we read, or are too big.",
        next="Check projects/knowledge.yaml against the repository and correct the paths.",
    ),
    "knowledge_sync_failed": LogEvent(
        "warning",
        what="Could not read {workspace}'s product documents ({code}).",
        explains="code",
    ),
    "knowledge_sync_crashed": LogEvent(
        "error",
        what="Reading {workspace}'s product documents stopped with an unexpected {kind} error.",
        why="Something in our own code went wrong; the other projects were still read.",
        next="Read the lines just before this one; the next round is in 6 hours or at the next start.",
    ),
    "knowledge_search_failed": LogEvent(
        "warning",
        what="The chat could not search {workspace}'s product documents ({code}).",
        why="The laptop database did not answer; the chat answered from the dashboard's numbers only.",
        next="Check the laptop database is running.",
    ),
    "monitoring_round_failed": LogEvent(
        "warning",
        what="One round of the Monitoring agents could not run.",
        why="It stopped with an unexpected {kind} error.",
        next="The next round runs in 5 minutes; phone alerts for downtime do not depend on it. "
        + _SEE_DASHBOARD,
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
