"""The revenue shapes: Subscription, Payment and Refund (METRICS §5).

Every payment connector (Paddle, Stripe, later RevenueCat) maps its vendor
objects to these shapes; ``core/metrics`` computes every number from them.
They were drawn from Paddle and Stripe together (Rulebook §2.4, ADR-019).

Money is always integer minor units plus an ISO currency, never a float. The
shapes hold vendor ids and amounts only: no names, emails or addresses, so a
revenue row is ``internal`` data, never ``customer-pii``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_CURRENCY = r"^[A-Z]{3}$"
_VENDOR_ID = r"^[A-Za-z0-9_\-]{1,128}$"
_SOURCE = r"^[a-z][a-z0-9_]{0,63}$"
_VENDOR_CODE = r"^[a-z0-9_]{1,64}$"


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Interval(StrEnum):
    """The unit of a billing cycle."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class BillingCycle(_Shape):
    """How often a subscription bills: every ``frequency`` ``interval``s."""

    interval: Interval
    frequency: int = Field(ge=1, le=366)


class SubscriptionStatus(StrEnum):
    """Where a subscription is. ``incomplete`` = first payment never succeeded (Stripe)."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    PAUSED = "paused"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class Subscription(_Shape):
    """One subscription as the vendor sees it now.

    ``recurring_minor`` is what one billing cycle costs: excluding tax, after
    discounts, before refunds. ``None`` means the connector could not work it
    out exactly (an unusual price or tax setup); metrics then say "unknown"
    instead of guessing.
    """

    source: str = Field(pattern=_SOURCE)
    provider_id: str = Field(pattern=_VENDOR_ID)
    customer_ref: str = Field(pattern=_VENDOR_ID)
    status: SubscriptionStatus
    cycle: BillingCycle
    currency: str = Field(pattern=_CURRENCY)
    recurring_minor: int | None = Field(ge=0)
    started_at: AwareDatetime | None = None
    canceled_at: AwareDatetime | None = None
    trial_ends_at: AwareDatetime | None = None
    vendor_updated_at: AwareDatetime


class PaymentStatus(StrEnum):
    """How one payment attempt ended (or that it has not ended yet)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    PENDING = "pending"


class Payment(_Shape):
    """One attempt to collect money. A retried charge is several attempts.

    ``amount_minor`` is what the attempt tried to collect, tax included;
    ``tax_minor`` is the tax inside it.
    """

    source: str = Field(pattern=_SOURCE)
    provider_id: str = Field(pattern=_VENDOR_ID)
    transaction_ref: str = Field(pattern=_VENDOR_ID)
    subscription_ref: str | None = Field(default=None, pattern=_VENDOR_ID)
    customer_ref: str | None = Field(default=None, pattern=_VENDOR_ID)
    status: PaymentStatus
    currency: str = Field(pattern=_CURRENCY)
    amount_minor: int = Field(ge=0)
    tax_minor: int = Field(ge=0)
    error_code: str | None = Field(default=None, pattern=_VENDOR_CODE)
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def _tax_inside_amount(self) -> Payment:
        if self.tax_minor > self.amount_minor:
            raise ValueError("tax cannot be larger than the amount it is part of")
        return self


class RefundKind(StrEnum):
    """Money going back: a refund we gave, a chargeback, or a chargeback undone."""

    REFUND = "refund"
    CHARGEBACK = "chargeback"
    CHARGEBACK_REVERSAL = "chargeback_reversal"


class RefundStatus(StrEnum):
    """Only ``approved`` money has actually moved."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVERSED = "reversed"


class Refund(_Shape):
    """One refund or chargeback against a payment's transaction (tax included)."""

    source: str = Field(pattern=_SOURCE)
    provider_id: str = Field(pattern=_VENDOR_ID)
    kind: RefundKind
    status: RefundStatus
    transaction_ref: str = Field(pattern=_VENDOR_ID)
    subscription_ref: str | None = Field(default=None, pattern=_VENDOR_ID)
    currency: str = Field(pattern=_CURRENCY)
    amount_minor: int = Field(ge=0)
    tax_minor: int = Field(ge=0)
    created_at: AwareDatetime
    vendor_updated_at: AwareDatetime

    @model_validator(mode="after")
    def _tax_inside_amount(self) -> Refund:
        if self.tax_minor > self.amount_minor:
            raise ValueError("tax cannot be larger than the amount it is part of")
        return self


class RevenueBatch(_Shape):
    """Everything one sync fetched. ``fetched_at`` becomes the next sync's cursor."""

    source: str = Field(pattern=_SOURCE)
    subscriptions: tuple[Subscription, ...] = ()
    payments: tuple[Payment, ...] = ()
    refunds: tuple[Refund, ...] = ()
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def _one_source_no_duplicates(self) -> RevenueBatch:
        groups: tuple[tuple[str, tuple[Subscription | Payment | Refund, ...]], ...] = (
            ("subscription", self.subscriptions),
            ("payment", self.payments),
            ("refund", self.refunds),
        )
        for kind, items in groups:
            if any(item.source != self.source for item in items):
                raise ValueError(f"a {kind} from another source is in this batch")
            ids = [item.provider_id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"a {kind} appears twice in this batch")
        return self


class SourceErrorCode(StrEnum):
    """Why a source could not deliver. Safe to log, store and show."""

    UNAVAILABLE = "source.unavailable"
    REFUSED = "source.refused"
    AUTH = "source.auth"
    RATE_LIMITED = "source.rate_limited"
    BAD_RESPONSE = "source.bad_response"
    UNKNOWN_VALUE = "source.unknown_value"
    TOO_MUCH_DATA = "source.too_much_data"


class SourceError(Exception):
    """A source failed. ``code`` says why; the message never carries vendor data or keys.

    Where it failed travels with it, so a log line can say what, where and
    which try: ``detail_code`` is a more precise reason from a lower layer
    (``net.timeout`` under ``source.unavailable``), ``status`` the HTTP status,
    and :meth:`at` records the operation (``GET /subscriptions``) and attempt.
    """

    def __init__(
        self,
        code: SourceErrorCode,
        message: str,
        *,
        detail_code: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.detail_code = detail_code
        self.status = status
        self.operation: str | None = None
        self.attempt: int | None = None
        self.attempts: int | None = None

    @property
    def reason_code(self) -> str:
        """The most precise code known: the lower layer's if there is one."""
        return self.detail_code or self.code.value

    def at(self, operation: str, attempt: int | None = None, attempts: int | None = None) -> Self:
        """Record where this failed. The innermost caller wins: a set operation is never overwritten."""
        if self.operation is None:
            self.operation = operation
            self.attempt = attempt
            self.attempts = attempts
        return self


class RevenueSource(Protocol):
    """What every payment connector offers: one read-only fetch."""

    @property
    def source(self) -> str:
        """The connector id, e.g. ``paddle``."""
        ...

    async def fetch(self, *, payments_since: datetime | None, now: datetime) -> RevenueBatch:
        """Return every subscription, plus payments and refunds changed since ``payments_since``.

        ``None`` means fetch the whole history. Raises :class:`SourceError` only.
        """
        ...


class JsonGetter(Protocol):
    """A read-only JSON GET, given to a connector by the core (the one HTTP client)."""

    async def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """GET ``path`` with query ``params``; return the decoded JSON object.

        Raises :class:`SourceError` (or a subclass) only.
        """
        ...
