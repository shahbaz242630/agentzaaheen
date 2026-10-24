"""The shared record shapes: Event, Finding, Action and RunRecord.

These are the contract every connector, agent and core module agrees on.
Changing a field here is an SDK version bump.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

SDK_VERSION = "1.11.0"

_DOTTED_NAME = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class Trust(StrEnum):
    """Whether text was written by us (trusted) or by anyone else (untrusted)."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class DataClass(StrEnum):
    """How sensitive a piece of data is. Ordered from least to most sensitive."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CUSTOMER_PII = "customer-pii"

    @property
    def rank(self) -> int:
        """Position in the sensitivity order (higher = more sensitive)."""
        return _DATA_CLASS_ORDER.index(self)


_DATA_CLASS_ORDER: tuple[DataClass, ...] = (DataClass.PUBLIC, DataClass.INTERNAL, DataClass.CUSTOMER_PII)


def max_data_class(*classes: DataClass) -> DataClass:
    """Return the most sensitive class. A run's class can only go up, never down.

    With no inputs the answer is PUBLIC: nothing sensitive has been seen yet.
    """
    if not classes:
        return DataClass.PUBLIC
    return max(classes, key=lambda c: c.rank)


def combine_trust(*levels: Trust) -> Trust:
    """Return UNTRUSTED if any input is untrusted (taint spreads, never fades)."""
    return Trust.UNTRUSTED if Trust.UNTRUSTED in levels else Trust.TRUSTED


def make_idempotency_key(source: str, vendor_event_id: str | None, payload_digest_input: str = "") -> str:
    """Build the key that makes storing the same outside event twice impossible.

    Uses the vendor's own event id when it has one; otherwise a hash of the content.
    """
    if not source:
        raise ValueError("source must not be empty")
    if vendor_event_id:
        return f"{source}:{vendor_event_id}"
    digest = hashlib.sha256(payload_digest_input.encode("utf-8")).hexdigest()
    return f"{source}:sha256:{digest}"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Event(_Record):
    """A fact collected from the outside world, e.g. ``ticket.created``."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    project_id: UUID
    type: str = Field(pattern=_DOTTED_NAME, max_length=80)
    source: str = Field(min_length=1, max_length=64)
    vendor_event_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=400)
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    schema_version: int = Field(ge=1)
    payload: dict[str, object]
    trust: Trust
    data_class: DataClass
    subject_ref: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _received_after_occurred(self) -> Event:
        # Small clock skew between vendors and us is normal; a whole day is not.
        if (self.occurred_at - self.received_at).total_seconds() > 86_400:
            raise ValueError("occurred_at is more than a day after received_at")
        return self


class Severity(StrEnum):
    """P1 = wake the Chairman; P2 = batched hourly; P3 = in the briefing."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class FindingKind(StrEnum):
    """What sort of conclusion a finding is."""

    INCIDENT = "incident"
    RISK = "risk"
    OPPORTUNITY = "opportunity"
    INFO = "info"
    QUESTION = "question"


class FindingStatus(StrEnum):
    """Where a finding is in its life."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class Evidence(_Record):
    """One link from a finding back to its source. Exactly one field is set."""

    event_id: UUID | None = None
    url: str | None = Field(default=None, max_length=2048)
    metric_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _exactly_one(self) -> Evidence:
        set_fields = [v for v in (self.event_id, self.url, self.metric_id) if v is not None]
        if len(set_fields) != 1:
            raise ValueError("evidence must reference exactly one of event_id, url, metric_id")
        return self

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("evidence urls must be https")
        return value


class Finding(_Record):
    """Something an agent or collector concluded. Every finding cites evidence."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    project_id: UUID
    run_id: UUID | None = None
    kind: FindingKind
    severity: Severity
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(max_length=20_000)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    metric_refs: dict[str, str] = Field(default_factory=dict)
    trust: Trust
    data_class: DataClass
    status: FindingStatus = FindingStatus.OPEN


class ActionLevel(StrEnum):
    """Draft = prepared inside Yaqzan; Act = would change the outside world."""

    DRAFT = "draft"
    ACT = "act"


class ActionStatus(StrEnum):
    """Where an action is in its life."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"
    EXPIRED = "expired"


class Action(_Record):
    """An outward action an agent proposes. Nothing leaves until a human approves."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    project_id: UUID
    run_id: UUID
    tool: str = Field(pattern=_DOTTED_NAME, max_length=80)
    level: ActionLevel
    payload: dict[str, object]
    target_kind: str = Field(min_length=1, max_length=64)
    target_ref: str = Field(min_length=1, max_length=256)
    data_classes: tuple[DataClass, ...]
    leak_check_passed: bool
    idempotency_key: str = Field(min_length=1, max_length=400)
    status: ActionStatus = ActionStatus.PROPOSED
    approved_by: UUID | None = None
    approved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _approval_is_consistent(self) -> Action:
        approved_states = {ActionStatus.APPROVED, ActionStatus.SENT, ActionStatus.FAILED}
        has_approval = self.approved_by is not None and self.approved_at is not None
        if self.status in approved_states and not has_approval:
            raise ValueError(f"status {self.status} requires approved_by and approved_at")
        if self.status in approved_states and not self.leak_check_passed:
            raise ValueError("an action that failed the leak check can never be approved")
        if self.status in {ActionStatus.PROPOSED, ActionStatus.REJECTED} and has_approval:
            raise ValueError(f"status {self.status} cannot carry an approval")
        return self


class RunOutcome(StrEnum):
    """How an agent run ended. ``nothing_found`` and ``could_not_run`` are never merged."""

    OK = "ok"
    NOTHING_FOUND = "nothing_found"
    COULD_NOT_RUN = "could_not_run"
    BUDGET_EXCEEDED = "budget_exceeded"
    DENIED = "denied"


class ModelInfo(_Record):
    """Exactly which model answered, so any run can be reproduced on paper."""

    tier: int = Field(ge=0, le=3)
    model_id: str = Field(max_length=128)
    quant: str | None = Field(default=None, max_length=32)
    file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(max_length=64)


class RunRecord(_Record):
    """The record of one agent run."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    project_id: UUID
    agent: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    trigger_event_ids: tuple[UUID, ...] = ()
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: ModelInfo | None = None
    data_class_max: DataClass
    trust: Trust
    steps: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_minor_units: int = Field(ge=0)
    started_at: datetime
    duration_ms: int = Field(ge=0)
    outcome: RunOutcome
    error_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _failure_needs_code(self) -> RunRecord:
        failed = {RunOutcome.COULD_NOT_RUN, RunOutcome.BUDGET_EXCEEDED, RunOutcome.DENIED}
        if self.outcome in failed and not self.error_code:
            raise ValueError(f"outcome {self.outcome} requires an error_code")
        return self
