"""The store: Postgres access for every other core module (schema, migrations, repositories).

What it does:
    Holds one connection pool (:class:`Database`), applies versioned migrations
    (:func:`migrate`, :func:`check_schema`) and saves/loads the SDK records,
    audit rows, sealed credentials, kill-switch state, registry switches
    and requests (ADR-030), the passkey (ADR-SEC-007), the revenue mirror
    (subscriptions, payments, refunds), each collector's sync state, the
    tenants and workspaces (archived, brought back or deleted for good, and the
    GitHub repository each reads, the scout's runs and checked advice, and each
    workspace's daily summaries with the Board's morning briefings, what each
    Claude call cost, and our customers' names as keyed fingerprints only),
    and each workspace's agents, switch and cap with
    the frozen role versions they run (ADR-039), and each agent run's start
    (F11), and the custom MCP servers the Chairman added with their last
    test (ADR-041). Every table has a ``tenant_id`` and sits
    behind a database wall (row-level security, forced): each transaction
    names its tenant, and for project-level tables its workspace, through
    :meth:`Database.scoped`; one that names none is refused. The app connects
    as a member of the ``agentzaaheen_app`` role, which may not update or delete
    audit rows and cannot get round the walls.

Public interface:
    Everything in ``__all__`` below. Errors leave as :class:`StoreError` with a
    reason code and never carry row values.

How to test:
    Start the throwaway dev database, then run the integration tests:
    ``uv run python -m tools.dev_db up`` and ``uv run pytest tests/integration``.

Must never:
    Format values into SQL; import the vault, the policy layer or any other
    core module except the SDK; store a plaintext secret; be bypassed (no other
    module reads tables directly); query a tenant table outside ``Database.scoped``.
"""

from agentzaaheen.core.store.agents import (
    AgentChange,
    AgentRow,
    AgentRows,
    NewAgent,
    Permission,
    RoleVersion,
    RoleVersionRows,
    WorkspaceSettings,
)
from agentzaaheen.core.store.audit import AuditRow, AuditRows
from agentzaaheen.core.store.bookmarks import MAX_BOOKMARKS, Bookmarks
from agentzaaheen.core.store.chats import MAX_TURNS, ChatRows, ChatThread, KeptTurn, SealedTurn, Sealer
from agentzaaheen.core.store.custom_mcp import MAX_SERVERS, CustomMcpRow, CustomMcpRows, McpTest, McpToolRow
from agentzaaheen.core.store.db import Database, DatabaseSettings, Scoped
from agentzaaheen.core.store.errors import StoreError, StoreErrorCode
from agentzaaheen.core.store.knowledge import PRODUCTS, KnowledgeRows, Passage
from agentzaaheen.core.store.migrations import LATEST_VERSION, MIGRATIONS, Migration, check_schema, migrate
from agentzaaheen.core.store.model_call_misses import MissRow, ModelCallMisses
from agentzaaheen.core.store.model_calls import CallRow, CallTokens, ModelCalls
from agentzaaheen.core.store.name_prints import AllNamePrints, NamePrints
from agentzaaheen.core.store.passkeys import PasskeyRow, PasskeyRows
from agentzaaheen.core.store.records import MAX_PAGE, Records
from agentzaaheen.core.store.registry_switches import (
    ItemKind,
    RegistrySwitchRows,
    RequestRow,
    RequestStatus,
    SwitchRow,
)
from agentzaaheen.core.store.revenue import MAX_ROWS, RevenueRows, SyncState
from agentzaaheen.core.store.run_starts import RUN_INTERRUPTED, MissedRun, RunCalls, RunStart, RunStarts
from agentzaaheen.core.store.scout import INTERRUPTED, ScoutRun, ScoutRuns
from agentzaaheen.core.store.sealed import (
    Access,
    AllSealedSecrets,
    CredentialDates,
    SealedRow,
    SealedSecrets,
    SlotKey,
)
from agentzaaheen.core.store.summaries import (
    BriefingRow,
    Briefings,
    BriefingState,
    DailySummaries,
    SummaryRow,
)
from agentzaaheen.core.store.switches import EngagedSwitch, KillSwitchRows, SwitchScope
from agentzaaheen.core.store.workspaces import (
    Archived,
    StateChange,
    WorkspaceLifecycle,
    WorkspaceRow,
    WorkspaceRows,
    WorkspaceSources,
)

__all__ = [
    "INTERRUPTED",
    "LATEST_VERSION",
    "MAX_BOOKMARKS",
    "MAX_PAGE",
    "MAX_ROWS",
    "MAX_SERVERS",
    "MAX_TURNS",
    "MIGRATIONS",
    "PRODUCTS",
    "RUN_INTERRUPTED",
    "Access",
    "AgentChange",
    "AgentRow",
    "AgentRows",
    "AllNamePrints",
    "AllSealedSecrets",
    "Archived",
    "AuditRow",
    "AuditRows",
    "Bookmarks",
    "BriefingRow",
    "BriefingState",
    "Briefings",
    "CallRow",
    "CallTokens",
    "ChatRows",
    "ChatThread",
    "CredentialDates",
    "CustomMcpRow",
    "CustomMcpRows",
    "DailySummaries",
    "Database",
    "DatabaseSettings",
    "EngagedSwitch",
    "ItemKind",
    "KeptTurn",
    "KillSwitchRows",
    "KnowledgeRows",
    "McpTest",
    "McpToolRow",
    "Migration",
    "MissRow",
    "MissedRun",
    "ModelCallMisses",
    "ModelCalls",
    "NamePrints",
    "NewAgent",
    "Passage",
    "PasskeyRow",
    "PasskeyRows",
    "Permission",
    "Records",
    "RegistrySwitchRows",
    "RequestRow",
    "RequestStatus",
    "RevenueRows",
    "RoleVersion",
    "RoleVersionRows",
    "RunCalls",
    "RunStart",
    "RunStarts",
    "Scoped",
    "ScoutRun",
    "ScoutRuns",
    "SealedRow",
    "SealedSecrets",
    "SealedTurn",
    "Sealer",
    "SlotKey",
    "StateChange",
    "StoreError",
    "StoreErrorCode",
    "SummaryRow",
    "SwitchRow",
    "SwitchScope",
    "SyncState",
    "WorkspaceLifecycle",
    "WorkspaceRow",
    "WorkspaceRows",
    "WorkspaceSettings",
    "WorkspaceSources",
    "check_schema",
    "migrate",
]
