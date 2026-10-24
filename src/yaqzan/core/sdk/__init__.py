"""The SDK: the only part of the core that plugins may import.

What it does:
    Defines the shared shapes (events, findings, actions, run records, tool
    declarations, the revenue shapes Subscription/Payment/Refund and the
    protocols a connector implements), the small rules that travel with them
    (data class only goes up, taint spreads) and the one redacting logger
    every module and plugin uses, and the error catalogue that puts every
    reason code into plain English (:func:`explain`), the uptime shapes
    (an address's rule, its status and timing, never its page) and the
    backend health shapes (codes and numbers only), the repository bundle
    the scout reads (paths and untrusted file text, secret files never), what a
    project is built with (languages, and providers code found), and spans: :func:`span`
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
from yaqzan.core.sdk.events import LOG_EVENTS, LogEvent, elapsed_ms, say
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
from yaqzan.core.sdk.repo_files import (
    MAX_FILE_BYTES,
    MAX_FILE_CHARS,
    MAX_PATHS,
    MAX_TOTAL_CHARS,
    RepoBundle,
    RepoFile,
    is_plain_path,
    is_secret_path,
    pick_files,
)
from yaqzan.core.sdk.repo_profile import (
    MAX_LANGUAGES,
    MAX_MANIFEST_BYTES,
    PROVIDERS,
    Language,
    Provider,
    ProviderCategory,
    RepoProfile,
    detect,
    pick_manifests,
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
    "LOG_EVENTS",
    "MAX_FILE_BYTES",
    "MAX_FILE_CHARS",
    "MAX_LANGUAGES",
    "MAX_LOG_CHARS",
    "MAX_LOG_LINES",
    "MAX_MANIFEST_BYTES",
    "MAX_PATHS",
    "MAX_TOTAL_CHARS",
    "PROJECT_REF",
    "PROVIDERS",
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
    "Language",
    "LogEvent",
    "LogSink",
    "ModelInfo",
    "Payment",
    "PaymentStatus",
    "ProjectState",
    "Provider",
    "ProviderCategory",
    "Refund",
    "RefundKind",
    "RefundStatus",
    "RepoBundle",
    "RepoFile",
    "RepoHealth",
    "RepoProfile",
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
    "detect",
    "elapsed_ms",
    "error_type",
    "event",
    "explain",
    "failed",
    "get_logger",
    "install_log_sink",
    "install_tracing",
    "is_plain_path",
    "is_secret_path",
    "is_sensitive_key",
    "judge",
    "log_digest",
    "make_failed_step_log",
    "make_idempotency_key",
    "max_data_class",
    "note",
    "pick_files",
    "pick_manifests",
    "redact",
    "redact_text",
    "say",
    "span",
    "trace_ids",
    "traceparent",
]
