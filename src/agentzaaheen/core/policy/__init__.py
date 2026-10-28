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

from agentzaaheen.core.policy.access import (
    INCREASES_RISK,
    REDUCES_RISK,
    STEP_UP_MAX_AGE,
    AccessDecision,
    Channel,
    Operation,
    Role,
    authorize,
)
from agentzaaheen.core.policy.agents import (
    SKIPPED_REASONS,
    AgentControl,
    AgentVerdict,
    RunPermit,
    RunRefusal,
    RunsReport,
    SkippedRun,
    WorkspaceAgents,
)
from agentzaaheen.core.policy.audit import (
    GENESIS_HASH,
    AuditDraft,
    AuditEntry,
    AuditOutcome,
    ChainAnchor,
    ChainReport,
    append,
    verify_chain,
)
from agentzaaheen.core.policy.chats import ChatControl
from agentzaaheen.core.policy.claude_key import (
    ClaudeKeyControl,
    ClaudeKeyStatus,
    ClaudeKeyVerdict,
    key_problem,
)
from agentzaaheen.core.policy.custom_mcp import RESERVED_NAMES, CustomMcpControl, McpVerdict
from agentzaaheen.core.policy.gate import (
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
from agentzaaheen.core.policy.ledger import Ledger, load_kill_switches
from agentzaaheen.core.policy.model_key import (
    ModelKeyControl,
    ModelKeyStatus,
    ModelKeyVerdict,
    model_key_problem,
)
from agentzaaheen.core.policy.passkey_approvals import PasskeyApprovals
from agentzaaheen.core.policy.registry_control import (
    OUTSIDE_KINDS,
    Pending,
    RegistryControl,
    Standing,
    Verdict,
)
from agentzaaheen.core.policy.scout import STALE_AFTER, ScoutControl, ScoutVerdict
from agentzaaheen.core.policy.server_token import (
    ServerTokenControl,
    ServerTokenStatus,
    ServerTokenVerdict,
    ServiceToken,
    token_problem,
)
from agentzaaheen.core.policy.summaries import SummaryControl
from agentzaaheen.core.policy.workspaces import Added, LifecycleVerdict, Removal, WorkspaceControl, slug_of

__all__ = [
    "GENESIS_HASH",
    "INCREASES_RISK",
    "MAX_TOOLS_PER_AGENT",
    "OUTSIDE_KINDS",
    "REDUCES_RISK",
    "RESERVED_NAMES",
    "SKIPPED_REASONS",
    "STALE_AFTER",
    "STEP_UP_MAX_AGE",
    "AccessDecision",
    "Added",
    "AgentControl",
    "AgentPolicy",
    "AgentVerdict",
    "AuditDraft",
    "AuditEntry",
    "AuditOutcome",
    "Budget",
    "ChainAnchor",
    "ChainReport",
    "Channel",
    "ChatControl",
    "ClaudeKeyControl",
    "ClaudeKeyStatus",
    "ClaudeKeyVerdict",
    "CustomMcpControl",
    "Decision",
    "DenyReason",
    "Effect",
    "GateChecks",
    "KillSwitches",
    "Ledger",
    "LifecycleVerdict",
    "McpVerdict",
    "ModelKeyControl",
    "ModelKeyStatus",
    "ModelKeyVerdict",
    "Operation",
    "PasskeyApprovals",
    "Pending",
    "ProjectPolicy",
    "RegistryControl",
    "Removal",
    "Role",
    "RunPermit",
    "RunRefusal",
    "RunState",
    "RunsReport",
    "ScoutControl",
    "ScoutVerdict",
    "ServerTokenControl",
    "ServerTokenStatus",
    "ServerTokenVerdict",
    "ServiceToken",
    "SkippedRun",
    "Standing",
    "SummaryControl",
    "Target",
    "ToolCall",
    "Verdict",
    "WorkspaceAgents",
    "WorkspaceControl",
    "append",
    "audit_draft",
    "authorize",
    "decide",
    "key_problem",
    "load_kill_switches",
    "model_key_problem",
    "slug_of",
    "token_problem",
    "verify_chain",
]
