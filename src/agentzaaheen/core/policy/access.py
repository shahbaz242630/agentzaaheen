"""Who may do what, from which channel (SECURITY §7, threats AU-2 and AG-7).

The rule is about the *direction of risk*:
    * operations that reduce risk (reject, snooze, pause, freeze, stop) may come
      from any channel the Chairman uses, including Telegram and voice;
    * operations that increase risk (approve, resume, promote knowledge, change
      policy, grant access to personal data) need the dashboard **and** a
      passkey step-up no older than 15 minutes.

Claude (``ai-operator``) may only read, and only over MCP. It can never
approve, resume, promote or edit policy. Anything not listed here is denied.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

STEP_UP_MAX_AGE = timedelta(minutes=15)


class Role(StrEnum):
    """Who is asking."""

    CHAIRMAN = "chairman"
    VIEWER = "viewer"
    AI_OPERATOR = "ai-operator"
    AGENT_AUDITOR = "agent-auditor"


class Channel(StrEnum):
    """Where the request came from. Only the dashboard is high-assurance."""

    DASHBOARD = "dashboard"
    TELEGRAM = "telegram"
    VOICE = "voice"
    NTFY = "ntfy"
    MCP = "mcp"
    SYSTEM = "system"


class Operation(StrEnum):
    """Things a person or operator can ask Agent Zaaheen to do."""

    VIEW = "view"
    REJECT_ACTION = "reject_action"
    SNOOZE = "snooze"
    PAUSE_AGENT = "pause_agent"
    FREEZE_PROJECT = "freeze_project"
    STOP_ALL = "stop_all"
    APPROVE_ACTION = "approve_action"
    RESUME = "resume"
    PROMOTE_KNOWLEDGE = "promote_knowledge"
    EDIT_POLICY = "edit_policy"
    GRANT_PII_ACCESS = "grant_pii_access"
    SWITCH_OFF = "switch_off"  # a registry item (ADR-030)
    REQUEST_ON = "request_on"  # ask for a registry item to be switched on
    WITHDRAW_REQUEST = "withdraw_request"
    REFUSE_REQUEST = "refuse_request"
    APPROVE_REGISTRY = "approve_registry"  # switch a registry item on (ADR-SEC-007): needs a fresh passkey
    ASSIGN_AGENT = "assign_agent"  # give a workspace an agent: it starts off, with no cap (ADR-039)
    LOWER_CAP = "lower_cap"  # a lower spending cap, for an agent or a workspace
    RAISE_CAP = "raise_cap"  # a higher (or first) spending cap: needs a fresh passkey
    # Removing a project (ADR-039). Archiving stops everything but deletes the saved keys, which cannot be
    # undone, so it needs a fresh passkey like the other two.
    ARCHIVE_WORKSPACE = "archive_workspace"
    ADD_WORKSPACE = "add_workspace"  # a new project: nothing in it is switched on and it cannot spend
    RESTORE_WORKSPACE = "restore_workspace"
    ERASE_WORKSPACE = "erase_workspace"  # delete an archived workspace's data for good
    ASK_SCOUT = "ask_scout"  # read a workspace's repository and write advice; nothing switches on
    # Custom MCP servers (ADR-041 slice 1): adding is only a request, a test only lists tools.
    ADD_CUSTOM_MCP = "add_custom_mcp"
    TEST_CUSTOM_MCP = "test_custom_mcp"
    REMOVE_CUSTOM_MCP = "remove_custom_mcp"


REDUCES_RISK = frozenset(
    {
        Operation.VIEW,
        Operation.REJECT_ACTION,
        Operation.SNOOZE,
        Operation.PAUSE_AGENT,
        Operation.FREEZE_PROJECT,
        Operation.STOP_ALL,
        Operation.SWITCH_OFF,
        # Asking changes nothing; only approving the request does (ADR-030).
        Operation.REQUEST_ON,
        Operation.WITHDRAW_REQUEST,
        Operation.REFUSE_REQUEST,
        # A new agent is switched off and cannot spend: nothing runs until a passkey turns it on.
        Operation.ASSIGN_AGENT,
        Operation.LOWER_CAP,
        # A new workspace likewise: no agents, switched off, no cap (ADR-039 amended 24 Oct).
        Operation.ADD_WORKSPACE,
        # The scout only reads and advises: its advice switches nothing on (ADR-039, slice 5b-2).
        Operation.ASK_SCOUT,
        # A custom MCP server added is a request nothing can use; a test sends none of our keys or text and
        # only lists its tools; removing takes power away (ADR-041). Approving one will need a passkey.
        Operation.ADD_CUSTOM_MCP,
        Operation.TEST_CUSTOM_MCP,
        Operation.REMOVE_CUSTOM_MCP,
    }
)
INCREASES_RISK = frozenset(set(Operation) - REDUCES_RISK)

_CHAIRMAN_CHANNELS = frozenset({Channel.DASHBOARD, Channel.TELEGRAM, Channel.VOICE, Channel.NTFY})


class AccessDecision(BaseModel):
    """Yes or no, with a reason code that is safe to log and show."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str


def _deny(reason: str) -> AccessDecision:
    return AccessDecision(allowed=False, reason=f"access.{reason}")


def _step_up_fresh(step_up_at: datetime | None, now: datetime) -> bool:
    if step_up_at is None or step_up_at.tzinfo is None or now.tzinfo is None:
        return False
    age = now - step_up_at
    return timedelta(0) <= age <= STEP_UP_MAX_AGE


def authorize(  # noqa: PLR0911  # one return per role, read top to bottom
    role: Role,
    operation: Operation,
    channel: Channel,
    *,
    step_up_at: datetime | None,
    now: datetime,
) -> AccessDecision:
    """Decide whether ``role`` may perform ``operation`` arriving on ``channel``."""
    if role is Role.AI_OPERATOR:
        if operation is Operation.VIEW and channel is Channel.MCP:
            return AccessDecision(allowed=True, reason="access.ai_operator_read")
        return _deny("ai_operator_read_only")
    if role is Role.VIEWER:
        if operation is Operation.VIEW and channel is Channel.DASHBOARD:
            return AccessDecision(allowed=True, reason="access.viewer_read")
        return _deny("viewer_read_only")
    if role is Role.AGENT_AUDITOR:
        if operation is Operation.PAUSE_AGENT and channel is Channel.SYSTEM:
            return AccessDecision(allowed=True, reason="access.auditor_pause")
        return _deny("auditor_pause_only")
    if role is not Role.CHAIRMAN:
        return _deny("unknown_role")
    return _authorize_chairman(operation, channel, step_up_at, now)


def _authorize_chairman(
    operation: Operation, channel: Channel, step_up_at: datetime | None, now: datetime
) -> AccessDecision:
    if channel not in _CHAIRMAN_CHANNELS:
        return _deny("channel_not_for_chairman")
    if operation in REDUCES_RISK:
        return AccessDecision(allowed=True, reason="access.reduces_risk")
    if channel is not Channel.DASHBOARD:
        return _deny("needs_dashboard")
    if not _step_up_fresh(step_up_at, now):
        return _deny("needs_fresh_passkey")
    return AccessDecision(allowed=True, reason="access.step_up_ok")
