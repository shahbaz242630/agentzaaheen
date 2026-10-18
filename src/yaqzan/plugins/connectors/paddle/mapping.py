"""Paddle JSON → the SDK's Subscription, Payment and Refund.

Vendor JSON is untrusted. Only typed fields survive: ids, known enum values,
integer amounts and times. Free text (names, emails, notes, custom data) is
never read. A value we have never seen raises ``SourceError(UNKNOWN_VALUE)``
so the sync fails loudly instead of guessing. A price set-up we cannot turn
into an exact monthly amount gives ``recurring_minor=None`` ("unknown").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from yaqzan.core.sdk import (
    BillingCycle,
    Interval,
    Payment,
    PaymentStatus,
    Refund,
    RefundKind,
    RefundStatus,
    SourceError,
    SourceErrorCode,
    Subscription,
    SubscriptionStatus,
)

SOURCE = "paddle"

Json = Mapping[str, object]

_SUBSCRIPTION_STATUS = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "paused": SubscriptionStatus.PAUSED,
    "canceled": SubscriptionStatus.CANCELED,
}
_PAYMENT_STATUS = {
    "captured": PaymentStatus.SUCCEEDED,
    "error": PaymentStatus.FAILED,
    "dropped": PaymentStatus.FAILED,
    "canceled": PaymentStatus.CANCELED,
    "authorized": PaymentStatus.PENDING,
    "authorized_flagged": PaymentStatus.PENDING,
    "action_required": PaymentStatus.PENDING,
    "pending_no_action_required": PaymentStatus.PENDING,
    "created": PaymentStatus.PENDING,
    "unknown": PaymentStatus.PENDING,
}
_REFUND_KIND: dict[str, RefundKind | None] = {
    "refund": RefundKind.REFUND,
    "chargeback": RefundKind.CHARGEBACK,
    "chargeback_reverse": RefundKind.CHARGEBACK_REVERSAL,
    # Credits and chargeback warnings move no money back to the customer.
    "credit": None,
    "credit_reverse": None,
    "chargeback_warning": None,
    "chargeback_warning_reverse": None,
}
_REFUND_STATUS = {
    "pending_approval": RefundStatus.PENDING,
    "approved": RefundStatus.APPROVED,
    "rejected": RefundStatus.REJECTED,
    "reversed": RefundStatus.REVERSED,
}
_COUNTED_ITEM_STATUSES = frozenset({"active", "trialing"})
_ERROR_CODE_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


# ---------------------------------------------------------------- field readers


def _bad(what: str) -> SourceError:
    return SourceError(SourceErrorCode.BAD_RESPONSE, f"paddle sent an unusable {what}")


def obj(value: object, what: str) -> Json:
    """``value`` as a JSON object, or raise."""
    if not isinstance(value, Mapping):
        raise _bad(what)
    return value


def objects(value: object, what: str) -> list[Json]:
    """``value`` as a list of JSON objects, or raise."""
    if not isinstance(value, list):
        raise _bad(what)
    return [obj(item, what) for item in value]


def text(data: Json, key: str) -> str:
    """A required string field."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise _bad(key)
    return value


def optional_text(data: Json, key: str) -> str | None:
    """A string field that may be null or missing."""
    if data.get(key) is None:
        return None
    return text(data, key)


def amount(data: Json, key: str) -> int:
    """A minor-unit amount, which Paddle sends as a string of digits."""
    value = data.get(key)
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()):
        raise _bad(key)
    return int(value)


def time(data: Json, key: str) -> datetime:
    """A required RFC 3339 time with a timezone."""
    raw = text(data, key)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise _bad(key) from None
    if value.tzinfo is None:
        raise _bad(key)
    return value


def optional_time(data: Json, key: str) -> datetime | None:
    """A time field that may be null or missing."""
    if data.get(key) is None:
        return None
    return time(data, key)


def known(table: Mapping[str, object], raw: str, what: str) -> None:
    """Raise ``UNKNOWN_VALUE`` if ``raw`` is not one of the values we map."""
    if raw not in table:
        raise SourceError(SourceErrorCode.UNKNOWN_VALUE, f"paddle sent a {what} we do not know")


def cycle(data: object) -> BillingCycle:
    """A Paddle ``billing_cycle`` object."""
    value = obj(data, "billing_cycle")
    interval = text(value, "interval")
    known({i.value: i for i in Interval}, interval, "billing interval")
    frequency = value.get("frequency")
    if not isinstance(frequency, int) or isinstance(frequency, bool):
        raise _bad("frequency")
    return BillingCycle(interval=Interval(interval), frequency=frequency)


# ---------------------------------------------------------------- subscriptions


def subscription(data: Json, discounts: Mapping[str, Json], now: datetime) -> Subscription:
    """Map one Paddle subscription. ``discounts`` holds the discount objects by id."""
    status = text(data, "status")
    known(_SUBSCRIPTION_STATUS, status, "subscription status")
    sub_cycle = cycle(data.get("billing_cycle"))
    currency = text(data, "currency_code")
    items = [item for item in objects(data.get("items"), "items") if _counted(item)]
    return Subscription(
        source=SOURCE,
        provider_id=text(data, "id"),
        customer_ref=text(data, "customer_id"),
        status=_SUBSCRIPTION_STATUS[status],
        cycle=sub_cycle,
        currency=currency,
        recurring_minor=recurring_minor(
            items,
            sub_cycle=sub_cycle,
            currency=currency,
            sub_discount=data.get("discount"),
            discounts=discounts,
            now=now,
        ),
        started_at=optional_time(data, "started_at"),
        canceled_at=optional_time(data, "canceled_at"),
        trial_ends_at=_trial_end(items),
        vendor_updated_at=time(data, "updated_at"),
    )


def _counted(item: Json) -> bool:
    return item.get("recurring") is True and item.get("status") in _COUNTED_ITEM_STATUSES


def _trial_end(items: Sequence[Json]) -> datetime | None:
    ends = [
        optional_time(obj(i["trial_dates"], "trial_dates"), "ends_at") for i in items if i.get("trial_dates")
    ]
    present = [end for end in ends if end is not None]
    return max(present) if present else None


def _item_amount(item: Json, sub_cycle: BillingCycle, currency: str) -> int | None:
    price = obj(item.get("price"), "price")
    if cycle(price.get("billing_cycle")) != sub_cycle or price.get("tax_mode") != "external":
        return None
    unit = obj(price.get("unit_price"), "unit_price")
    if text(unit, "currency_code") != currency:
        return None  # a country override applies; we cannot tell which without the address
    quantity = item.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise _bad("quantity")
    return amount(unit, "amount") * quantity


def recurring_minor(  # noqa: PLR0913  # all of these decide one number
    items: Sequence[Json],
    *,
    sub_cycle: BillingCycle,
    currency: str,
    sub_discount: object,
    discounts: Mapping[str, Json],
    now: datetime,
) -> int | None:
    """One cycle's price: excluding tax, after any discount in force at ``now``.

    ``None`` when it cannot be exact: tax-inclusive or account-default tax,
    an item on another cycle, a country price override, or a discount we
    cannot read.
    """
    amounts = [_item_amount(item, sub_cycle, currency) for item in items]
    if any(a is None for a in amounts):
        return None
    lines = [(item, a) for item, a in zip(items, amounts, strict=True) if a is not None]
    total = sum(a for _, a in lines)
    if sub_discount is None:
        return total
    window = obj(sub_discount, "discount")
    starts, ends = optional_time(window, "starts_at"), optional_time(window, "ends_at")
    if (starts is not None and now < starts) or (ends is not None and now >= ends):
        return total
    discount = discounts.get(text(window, "id"))
    if discount is None:
        return None
    off = discount_off(discount, lines, currency)
    return None if off is None else total - off


def _eligible(discount: Json, item: Json) -> bool:
    restrict = discount.get("restrict_to")
    if restrict is None:
        return True
    price = obj(item.get("price"), "price")
    allowed = {entry for entry in restrict if isinstance(entry, str)} if isinstance(restrict, list) else set()
    return price.get("id") in allowed or price.get("product_id") in allowed


def discount_off(discount: Json, lines: Sequence[tuple[Json, int]], currency: str) -> int | None:
    """How much a Paddle discount takes off these lines, or ``None`` if we cannot say exactly."""
    eligible = [(item, a) for item, a in lines if _eligible(discount, item)]
    base = sum(a for _, a in eligible)
    kind = discount.get("type")
    raw = discount.get("amount")
    if not isinstance(raw, str):
        return None
    if kind == "percentage":
        return _percent_off(base, raw, lines=len(eligible))
    if not (raw.isascii() and raw.isdigit()) or discount.get("currency_code") != currency:
        return None
    if kind == "flat":
        return min(int(raw), base)
    if kind == "flat_per_seat":
        return sum(min(int(raw) * _quantity(item), a) for item, a in eligible)
    return None


def _percent_off(base: int, raw: str, *, lines: int) -> int | None:
    try:
        percent = Decimal(raw)
    except InvalidOperation:
        return None
    if not percent.is_finite() or not 0 <= percent <= 100:  # a percentage
        return None
    exact = Decimal(base) * percent / 100
    if lines > 1 and exact != exact.to_integral_value():
        return None  # Paddle may round per line; we cannot know, so we do not claim an exact number
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _quantity(item: Json) -> int:
    quantity = item.get("quantity")
    return quantity if isinstance(quantity, int) else 0


# ---------------------------------------------------------------- payments and refunds


def payments(transaction: Json) -> list[Payment]:
    """Every payment attempt on one Paddle transaction."""
    totals = obj(obj(transaction.get("details"), "details").get("totals"), "totals")
    tax = amount(totals, "tax")
    currency = text(transaction, "currency_code")
    out: list[Payment] = []
    for attempt in objects(transaction.get("payments") or [], "payments"):
        status = text(attempt, "status")
        known(_PAYMENT_STATUS, status, "payment status")
        charged = amount(attempt, "amount")
        out.append(
            Payment(
                source=SOURCE,
                provider_id=text(attempt, "payment_attempt_id"),
                transaction_ref=text(transaction, "id"),
                subscription_ref=optional_text(transaction, "subscription_id"),
                customer_ref=optional_text(transaction, "customer_id"),
                status=_PAYMENT_STATUS[status],
                currency=currency,
                amount_minor=charged,
                tax_minor=min(tax, charged),
                error_code=_error_code(attempt.get("error_code")),
                occurred_at=optional_time(attempt, "captured_at") or time(attempt, "created_at"),
            )
        )
    return out


def _error_code(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str) and 0 < len(raw) <= 64 and set(raw) <= _ERROR_CODE_OK:
        return raw
    return "unrecognised"


def refund(adjustment: Json) -> Refund | None:
    """Map one Paddle adjustment; ``None`` for credits and warnings (no money moved back)."""
    action = text(adjustment, "action")
    known(_REFUND_KIND, action, "adjustment action")
    kind = _REFUND_KIND[action]
    if kind is None:
        return None
    status = text(adjustment, "status")
    known(_REFUND_STATUS, status, "adjustment status")
    totals = obj(adjustment.get("totals"), "totals")
    return Refund(
        source=SOURCE,
        provider_id=text(adjustment, "id"),
        kind=kind,
        status=_REFUND_STATUS[status],
        transaction_ref=text(adjustment, "transaction_id"),
        subscription_ref=optional_text(adjustment, "subscription_id"),
        currency=text(totals, "currency_code"),
        amount_minor=amount(totals, "total"),
        tax_minor=amount(totals, "tax"),
        created_at=time(adjustment, "created_at"),
        vendor_updated_at=time(adjustment, "updated_at"),
    )
