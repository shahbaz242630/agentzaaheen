"""The SDK: the only part of the core that plugins may import.

What it does:
    Defines the shared shapes (events, findings, actions, run records, tool
    declarations, the revenue shapes Subscription/Payment/Refund and the
    protocols a connector implements), the small rules that travel with them
    (data class only goes up, taint spreads) and the one redacting logger
    every module and plugin uses, and the error catalogue that puts every
    reason code into plain English (:func:`explain`).

Public interface:
    Everything listed in ``__all__`` below. Nothing else is public.

How to test:
    ``uv run pytest tests/unit/test_sdk_models.py tests/unit/test_sdk_log.py``
    ``uv run pytest tests/unit/test_sdk_revenue.py tests/unit/test_sdk_code.py``

Must never:
    Import any other core module, touch the database, the network or secrets.
    Log through anything other than :func:`get_logger`.
"""

from yaqzan.core.sdk.code import REPO_PATTERN, BuildState, RepoHealth, RepoSource
from yaqzan.core.sdk.explain import KNOWN_CODES, Explanation, explain
from yaqzan.core.sdk.log import REDACTED, contains_sensitive, get_logger, redact, redact_text
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
from yaqzan.core.sdk.revenue import (
    BillingCycle,
    Interval,
    JsonGetter,
    Payment,
    PaymentStatus,
    Refund,
    RefundKind,
    RefundStatus,
    RevenueBatch,
    RevenueSource,
    SourceError,
    SourceErrorCode,
    Subscription,
    SubscriptionStatus,
)
from yaqzan.core.sdk.tools import ToolLevel, ToolSpec

__all__ = [
    "KNOWN_CODES",
    "REDACTED",
    "REPO_PATTERN",
    "SDK_VERSION",
    "Action",
    "ActionLevel",
    "ActionStatus",
    "BillingCycle",
    "BuildState",
    "DataClass",
    "Event",
    "Evidence",
    "Explanation",
    "Finding",
    "FindingKind",
    "FindingStatus",
    "Interval",
    "JsonGetter",
    "ModelInfo",
    "Payment",
    "PaymentStatus",
    "Refund",
    "RefundKind",
    "RefundStatus",
    "RepoHealth",
    "RepoSource",
    "RevenueBatch",
    "RevenueSource",
    "RunOutcome",
    "RunRecord",
    "Severity",
    "SourceError",
    "SourceErrorCode",
    "Subscription",
    "SubscriptionStatus",
    "ToolLevel",
    "ToolSpec",
    "Trust",
    "combine_trust",
    "contains_sensitive",
    "explain",
    "get_logger",
    "make_idempotency_key",
    "max_data_class",
    "redact",
    "redact_text",
]
