"""The policy layer: the only door agents have to tools, and who may do what.

What it does:
    ``decide`` judges every agent tool call from server state only: kill
    switches, allowlist, permission ladder, split-run rule, budgets, argument
    and ownership checks. ``authorize`` judges human and operator requests by
    role, channel and passkey freshness. ``append``/``verify_chain`` keep the
    hash-chained audit log that records every decision. ``Ledger`` stores that
    chain in Postgres and re-verifies it; ``load_kill_switches`` reads the
    switches and fails closed to "stop everything".

Public interface:
    Everything in ``__all__`` below. The MCP proxy that calls ``decide``
    arrives in Phase 2.

How to test:
    ``uv run pytest tests/unit/test_policy_gate.py tests/unit/test_policy_access.py
    tests/unit/test_policy_audit.py``; the ledger needs the dev database:
    ``uv run pytest tests/integration/test_policy_ledger.py``

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
from yaqzan.core.policy.ledger import Ledger, load_kill_switches

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
    "Ledger",
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
    "load_kill_switches",
    "verify_chain",
]
