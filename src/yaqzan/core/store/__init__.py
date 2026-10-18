"""The store: Postgres access for every other core module (schema, migrations, repositories).

What it does:
    Holds one connection pool (:class:`Database`), applies versioned migrations
    (:func:`migrate`, :func:`check_schema`) and saves/loads the SDK records,
    audit rows, sealed credentials, kill-switch state, the revenue mirror
    (subscriptions, payments, refunds) and each collector's sync state.
    Every table has a ``tenant_id``; every read is scoped by it. The app
    connects as a member of the ``yaqzan_app`` role, which may not update or
    delete audit rows.

Public interface:
    Everything in ``__all__`` below. Errors leave as :class:`StoreError` with a
    reason code and never carry row values.

How to test:
    Start the throwaway dev database, then run the integration tests:
    ``uv run python -m tools.dev_db up`` and ``uv run pytest tests/integration``.

Must never:
    Format values into SQL; import the vault, the policy layer or any other
    core module except the SDK; store a plaintext secret; be bypassed (no other
    module reads tables directly).
"""

from yaqzan.core.store.audit import AuditRow, AuditRows
from yaqzan.core.store.db import Database, DatabaseSettings
from yaqzan.core.store.errors import StoreError, StoreErrorCode
from yaqzan.core.store.migrations import LATEST_VERSION, MIGRATIONS, Migration, check_schema, migrate
from yaqzan.core.store.records import MAX_PAGE, Records
from yaqzan.core.store.revenue import MAX_ROWS, RevenueRows, SyncState
from yaqzan.core.store.sealed import SealedRow, SealedSecrets, SlotKey
from yaqzan.core.store.switches import EngagedSwitch, KillSwitchRows, SwitchScope

__all__ = [
    "LATEST_VERSION",
    "MAX_PAGE",
    "MAX_ROWS",
    "MIGRATIONS",
    "AuditRow",
    "AuditRows",
    "Database",
    "DatabaseSettings",
    "EngagedSwitch",
    "KillSwitchRows",
    "Migration",
    "Records",
    "RevenueRows",
    "SealedRow",
    "SealedSecrets",
    "SlotKey",
    "StoreError",
    "StoreErrorCode",
    "SwitchScope",
    "SyncState",
    "check_schema",
    "migrate",
]
