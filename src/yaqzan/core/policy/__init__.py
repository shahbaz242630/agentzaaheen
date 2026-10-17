"""The policy layer: the only door agents have to tools, and who may do what.

What it does:
    ``decide`` judges every agent tool call from server state only: kill
    switches, allowlist, permission ladder, split-run rule, budgets, argument
    and ownership checks. ``authorize`` judges human and operator requests by
    role, channel and passkey freshness. ``append``/``verify_chain`` keep the
    hash-chained audit log that records every decision.

Public interface:
    Everything in ``__all__`` below. Storage of audit rows and kill-switch
    state arrives with the store module; the MCP proxy that calls ``decide``
    arrives in Phase 2.

How to test:
    ``uv run pytest tests/unit/test_policy_gate.py tests/unit/test_policy_access.py
    tests/unit/test_policy_audit.py``

Must never:
    Let an LLM authorise anything; trust a target, permission or amount that
    came from model output; let anything above ``read`` execute without a
    human approval; allow by default when a check fails or is unknown.
"""

from yaqzan.core.policy.access import (
    INCREASES_RISK,
    REDUCES_RISK,
    STEP_UP_MAX_AGE,
    AccessDecision,
    Channel,
    Operation,
    Role,
    authorize,
)
from yaqzan.core.policy.audit import (
    GENESIS_HASH,
    AuditDraft,
    AuditEntry,
    AuditOutcome,
    ChainAnchor,
    ChainReport,
    append,
    verify_chain,
)
from yaqzan.core.policy.gate import (
    MAX_TOOLS_PER_AGENT,
    AgentPolicy,
    Budget,
    Decision,
    DenyReason,
    Effect,
    GateChecks,
    KillSwitches,
    ProjectPolicy,
    RunState,
    Target,
    ToolCall,
    audit_draft,
    decide,
)

__all__ = [
    "GENESIS_HASH",
    "INCREASES_RISK",
    "MAX_TOOLS_PER_AGENT",
    "REDUCES_RISK",
    "STEP_UP_MAX_AGE",
    "AccessDecision",
    "AgentPolicy",
    "AuditDraft",
    "AuditEntry",
    "AuditOutcome",
    "Budget",
    "ChainAnchor",
    "ChainReport",
    "Channel",
    "Decision",
    "DenyReason",
    "Effect",
    "GateChecks",
    "KillSwitches",
    "Operation",
    "ProjectPolicy",
    "Role",
    "RunState",
    "Target",
    "ToolCall",
    "append",
    "audit_draft",
    "authorize",
    "decide",
    "verify_chain",
]
