"""The SDK: the only part of the core that plugins may import.

What it does:
    Defines the shared shapes (events, findings, actions, run records, tool
    declarations, the revenue shapes Subscription/Payment/Refund and the
    protocols a connector implements), the small rules that travel with them
    (data class only goes up, taint spreads) and the one redacting logger
    every module and plugin uses, and the error catalogue that puts every
    reason code into plain English (:func:`explain`), the uptime shapes
    (an address's rule, its status and timing, never its page) and the
    backend health shapes (codes and numbers only), and spans: :func:`span`
    opens one, :func:`traceparent` names the current one, and every log line
    inside a span carries its IDs (ADR-032); :func:`install_log_sink` lets
    telemetry ship each redacted line.

Public interface:
    Everything listed in ``__all__`` below. Nothing else is public.

How to test:
    ``uv run pytest tests/unit/test_sdk_models.py tests/unit/test_sdk_log.py``
    ``uv run pytest tests/unit/test_sdk_revenue.py tests/unit/test_sdk_code.py``

Must never:
    Import any other core module, touch the database, the network or secrets.
    Log through anything other than :func:`get_logger`.
"""

from yaqzan.core.sdk.backend import (
    PROJECT_REF,
    BackendHealth,
    BackendSource,
    JsonReader,
    ProjectState,
    ServiceHealth,
)
from yaqzan.core.sdk.code import REPO_PATTERN, BuildState, RepoHealth, RepoSource
from yaqzan.core.sdk.explain import KNOWN_CODES, Explanation, explain
from yaqzan.core.sdk.log import (
    REDACTED,
    LogSink,
    contains_sensitive,
    get_logger,
    is_sensitive_key,
    redact,
    redact_text,
)
from yaqzan.core.sdk.log import install_sink as install_log_sink
from yaqzan.core.sdk.logs import (
    MAX_LOG_CHARS,
    MAX_LOG_LINES,
    BuildLogSource,
    BytesGetter,
    FailedStepLog,
    clean_log,
    log_digest,
    make_failed_step_log,
)
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
from yaqzan.core.sdk.trace import (
    ALLOWED_ATTRIBUTES,
    error_type,
    event,
    failed,
    note,
    span,
    trace_ids,
    traceparent,
)
from yaqzan.core.sdk.trace import install as install_tracing
from yaqzan.core.sdk.uptime import (
    Expect,
    SiteCheck,
    SiteRule,
    StatusAnswer,
    StatusGetter,
    UptimeReport,
    check_site,
    judge,
)

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "KNOWN_CODES",
    "MAX_LOG_CHARS",
    "MAX_LOG_LINES",
    "PROJECT_REF",
    "REDACTED",
    "REPO_PATTERN",
    "SDK_VERSION",
    "Action",
    "ActionLevel",
    "ActionStatus",
    "BackendHealth",
    "BackendSource",
    "BillingCycle",
    "BuildLogSource",
    "BuildState",
    "BytesGetter",
    "DataClass",
    "Event",
    "Evidence",
    "Expect",
    "Explanation",
    "FailedStepLog",
    "Finding",
    "FindingKind",
    "FindingStatus",
    "Interval",
    "JsonGetter",
    "JsonReader",
    "LogSink",
    "ModelInfo",
    "Payment",
    "PaymentStatus",
    "ProjectState",
    "Refund",
    "RefundKind",
    "RefundStatus",
    "RepoHealth",
    "RepoSource",
    "RevenueBatch",
    "RevenueSource",
    "RunOutcome",
    "RunRecord",
    "ServiceHealth",
    "Severity",
    "SiteCheck",
    "SiteRule",
    "SourceError",
    "SourceErrorCode",
    "StatusAnswer",
    "StatusGetter",
    "Subscription",
    "SubscriptionStatus",
    "ToolLevel",
    "ToolSpec",
    "Trust",
    "UptimeReport",
    "check_site",
    "clean_log",
    "combine_trust",
    "contains_sensitive",
    "error_type",
    "event",
    "explain",
    "failed",
    "get_logger",
    "install_log_sink",
    "install_tracing",
    "is_sensitive_key",
    "judge",
    "log_digest",
    "make_failed_step_log",
    "make_idempotency_key",
    "max_data_class",
    "note",
    "redact",
    "redact_text",
    "span",
    "trace_ids",
    "traceparent",
]
