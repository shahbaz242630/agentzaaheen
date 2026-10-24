"""The store: Postgres access for every other core module (schema, migrations, repositories).

What it does:
    Holds one connection pool (:class:`Database`), applies versioned migrations
    (:func:`migrate`, :func:`check_schema`) and saves/loads the SDK records,
    audit rows, sealed credentials, kill-switch state, registry switches
    and requests (ADR-030), the passkey (ADR-SEC-007), the revenue mirror
    (subscriptions, payments, refunds), each collector's sync state, and the
    tenants and workspaces (ADR-039). Every table has a ``tenant_id`` and sits
    behind a database wall (row-level security, forced): each transaction
    names its tenant, and for project-level tables its workspace, through
    :meth:`Database.scoped`; one that names none is refused. The app connects
    as a member of the ``yaqzan_app`` role, which may not update or delete
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

from yaqzan.core.store.audit import AuditRow, AuditRows
from yaqzan.core.store.db import Database, DatabaseSettings, Scoped
from yaqzan.core.store.errors import StoreError, StoreErrorCode
from yaqzan.core.store.migrations import LATEST_VERSION, MIGRATIONS, Migration, check_schema, migrate
from yaqzan.core.store.passkeys import PasskeyRow, PasskeyRows
from yaqzan.core.store.records import MAX_PAGE, Records
from yaqzan.core.store.registry_switches import (
    ItemKind,
    RegistrySwitchRows,
    RequestRow,
    RequestStatus,
    SwitchRow,
)
from yaqzan.core.store.revenue import MAX_ROWS, RevenueRows, SyncState
from yaqzan.core.store.sealed import Access, CredentialDates, SealedRow, SealedSecrets, SlotKey
from yaqzan.core.store.switches import EngagedSwitch, KillSwitchRows, SwitchScope
from yaqzan.core.store.workspaces import WorkspaceRow, WorkspaceRows

__all__ = [
    "LATEST_VERSION",
    "MAX_PAGE",
    "MAX_ROWS",
    "MIGRATIONS",
    "Access",
    "AuditRow",
    "AuditRows",
    "CredentialDates",
    "Database",
    "DatabaseSettings",
    "EngagedSwitch",
    "ItemKind",
    "KillSwitchRows",
    "Migration",
    "PasskeyRow",
    "PasskeyRows",
    "Records",
    "RegistrySwitchRows",
    "RequestRow",
    "RequestStatus",
    "RevenueRows",
    "Scoped",
    "SealedRow",
    "SealedSecrets",
    "SlotKey",
    "StoreError",
    "StoreErrorCode",
    "SwitchRow",
    "SwitchScope",
    "SyncState",
    "WorkspaceRow",
    "WorkspaceRows",
    "check_schema",
    "migrate",
]
