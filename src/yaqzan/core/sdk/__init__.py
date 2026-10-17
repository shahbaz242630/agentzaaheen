"""The SDK: the only part of the core that plugins may import.

What it does:
    Defines the shared shapes (events, findings, actions, run records) and the
    small rules that travel with them (data class only goes up, taint spreads).

Public interface:
    Everything listed in ``__all__`` below. Nothing else is public.

How to test:
    ``uv run pytest tests/unit/test_sdk_models.py``

Must never:
    Import any other core module, touch the database, the network or secrets.
"""

from yaqzan.core.sdk.models import (
    SDK_VERSION,
    Action,
    ActionLevel,
    ActionStatus,
    DataClass,
    Event,
    Evidence,
    Finding,
    FindingKind,
    FindingStatus,
    ModelInfo,
    RunOutcome,
    RunRecord,
    Severity,
    Trust,
    combine_trust,
    make_idempotency_key,
    max_data_class,
)

__all__ = [
    "SDK_VERSION",
    "Action",
    "ActionLevel",
    "ActionStatus",
    "DataClass",
    "Event",
    "Evidence",
    "Finding",
    "FindingKind",
    "FindingStatus",
    "ModelInfo",
    "RunOutcome",
    "RunRecord",
    "Severity",
    "Trust",
    "combine_trust",
    "make_idempotency_key",
    "max_data_class",
]
