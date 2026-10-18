"""A synthetic Paddle account: vendor JSON in Paddle's real shape, plus the SDK shapes it must map to.

Shapes copied from a read of the Zaaheen **sandbox** (2026-10-18) and Paddle's API
reference; every id, amount and time here is invented. No names, emails or
addresses appear anywhere, which is also what the mapping must never read.

``serve`` answers like Paddle's list endpoints, two items per page, so paging is
always exercised.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx

from yaqzan.core.sdk import (
    BillingCycle,
    Interval,
    Payment,
    PaymentStatus,
    Refund,
    RefundKind,
    RefundStatus,
    Subscription,
    SubscriptionStatus,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
PAGE_SIZE = 2
MONTH = {"interval": "month", "frequency": 1}
YEAR = {"interval": "year", "frequency": 1}


def pid(prefix: str, n: int) -> str:
    """A Paddle-shaped id: prefix + 26 lowercase characters."""
    return f"{prefix}_01{str(n).rjust(24, '0')}"


def at(day: int, hour: int = 9) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:00:00.000000Z"


def when(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def price(
    n: int, cents: str, currency: str = "USD", cycle: Mapping[str, object] = MONTH, tax_mode: str = "external"
) -> dict[str, object]:
    return {
        "id": pid("pri", n),
        "product_id": pid("pro", 1),
        "description": "free text we never read",
        "unit_price": {"amount": cents, "currency_code": currency},
        "billing_cycle": dict(cycle),
        "tax_mode": tax_mode,
        "trial_period": None,
        "unit_price_overrides": [],
        "custom_data": {"note": "ignored"},
    }


def item(
    item_price: dict[str, object], quantity: int = 1, status: str = "active", trial_ends: str | None = None
) -> dict[str, object]:
    return {
        "status": status,
        "quantity": quantity,
        "recurring": True,
        "price": item_price,
        "trial_dates": None if trial_ends is None else {"starts_at": at(1), "ends_at": trial_ends},
    }


def sub(
    n: int,
    customer: int,
    status: str,
    items: list[dict[str, object]],
    *,
    currency: str = "USD",
    cycle: Mapping[str, object] = MONTH,
    discount: dict[str, object] | None = None,
    canceled_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": pid("sub", n),
        "status": status,
        "customer_id": pid("ctm", customer),
        "address_id": pid("add", customer),
        "business_id": None,
        "currency_code": currency,
        "created_at": at(1),
        "updated_at": at(n),
        "started_at": at(1),
        "first_billed_at": at(1),
        "next_billed_at": None,
        "paused_at": None,
        "canceled_at": canceled_at,
        "collection_mode": "automatic",
        "billing_details": None,
        "current_billing_period": None,
        "billing_cycle": dict(cycle),
        "scheduled_change": None,
        "items": items,
        "custom_data": {"email": "never-read@example.invalid"},
        "management_urls": {"cancel": "https://example.invalid/cancel"},
        "discount": discount,
        "import_meta": None,
        "consent_requirements": [],
    }


SUBSCRIPTIONS = [
    sub(1, 1, "active", [item(price(1, "500"))]),
    sub(2, 2, "active", [item(price(2, "4800", cycle=YEAR))], cycle=YEAR),
    sub(3, 3, "trialing", [item(price(1, "500"), status="trialing", trial_ends=at(30))]),
    sub(4, 4, "canceled", [item(price(1, "500"))], canceled_at=at(4)),
    # Customer 1 again (a second subscription): counted once as a subscriber.
    sub(
        5,
        1,
        "past_due",
        [item(price(1, "500"))],
        discount={"id": pid("dsc", 1), "starts_at": at(1), "ends_at": None},
    ),
    sub(
        6,
        5,
        "active",
        [item(price(3, "400", currency="GBP"))],
        currency="GBP",
        discount={"id": pid("dsc", 2), "starts_at": None, "ends_at": None},
    ),
    sub(7, 6, "paused", [item(price(1, "500"))]),
]

DISCOUNTS: list[dict[str, object]] = [
    {
        "id": pid("dsc", 1),
        "status": "active",
        "type": "percentage",
        "amount": "20",
        "currency_code": None,
        "recur": True,
        "restrict_to": None,
        "code": "SPRING20",
    },
    {
        "id": pid("dsc", 2),
        "status": "active",
        "type": "flat",
        "amount": "100",
        "currency_code": "GBP",
        "recur": True,
        "restrict_to": None,
        "code": None,
    },
]


def attempt(
    n: int, status: str, cents: str, created: str, captured: str | None = None, error: str | None = None
) -> dict[str, object]:
    return {
        "payment_attempt_id": f"0000000{n}-aaaa-4bbb-8ccc-00000000000{n}",
        "stored_payment_method_id": f"0000000{n}-dddd-4eee-8fff-00000000000{n}",
        "payment_method_id": pid("paymtd", n),
        "amount": cents,
        "status": status,
        "error_code": error,
        "method_details": {"type": "card", "card": {"last4": "4242", "cardholder_name": "Never Read"}},
        "created_at": created,
        "captured_at": captured,
    }


def txn(
    n: int,
    status: str,
    sub_n: int | None,
    customer: int,
    payments: list[dict[str, object]],
    tax: str,
    total: str,
    updated: str,
    currency: str = "USD",
) -> dict[str, object]:
    return {
        "id": pid("txn", n),
        "status": status,
        "customer_id": pid("ctm", customer),
        "subscription_id": None if sub_n is None else pid("sub", sub_n),
        "currency_code": currency,
        "origin": "subscription_recurring",
        "created_at": at(1),
        "updated_at": updated,
        "billed_at": updated,
        "details": {
            "totals": {
                "subtotal": str(int(total) - int(tax)),
                "tax": tax,
                "discount": "0",
                "total": total,
                "grand_total": total,
                "currency_code": currency,
            }
        },
        "payments": payments,
        "checkout": {"url": "https://example.invalid/pay"},
        "billing_details": None,
    }


TRANSACTIONS = [
    txn(
        1,
        "completed",
        1,
        1,
        [
            attempt(1, "error", "525", at(10), error="declined"),
            attempt(2, "captured", "525", at(11), captured=at(11, 10)),
        ],
        tax="25",
        total="525",
        updated=at(11, 10),
    ),
    txn(
        2,
        "completed",
        2,
        2,
        [attempt(3, "captured", "5040", at(5), captured=at(5))],
        tax="240",
        total="5040",
        updated=at(5),
    ),
    txn(
        3,
        "past_due",
        5,
        1,
        [attempt(4, "error", "420", at(15), error="insufficient_funds")],
        tax="20",
        total="420",
        updated=at(15),
    ),
    txn(4, "draft", None, 1, [], tax="25", total="525", updated=at(2)),
    txn(
        5,
        "completed",
        6,
        5,
        [attempt(5, "captured", "360", at(12), captured=at(12))],
        tax="60",
        total="360",
        updated=at(12),
        currency="GBP",
    ),
]


def adjustment(
    n: int, action: str, status: str, txn_n: int, total: str, tax: str, created: str, currency: str = "USD"
) -> dict[str, object]:
    return {
        "id": pid("adj", n),
        "action": action,
        "type": "full",
        "transaction_id": pid("txn", txn_n),
        "subscription_id": pid("sub", 1),
        "customer_id": pid("ctm", 1),
        "reason": "free text from a person, never read",
        "currency_code": currency,
        "status": status,
        "items": [],
        "totals": {
            "subtotal": str(int(total) - int(tax)),
            "tax": tax,
            "total": total,
            "fee": "0",
            "earnings": "0",
            "currency_code": currency,
        },
        "created_at": created,
        "updated_at": created,
    }


ADJUSTMENTS = [
    adjustment(1, "refund", "approved", 2, "5040", "240", at(6)),
    adjustment(2, "credit", "approved", 1, "100", "0", at(7)),
    adjustment(3, "chargeback", "approved", 1, "525", "25", at(13)),
    adjustment(4, "chargeback_reverse", "approved", 1, "525", "25", at(16)),
    adjustment(5, "refund", "pending_approval", 1, "525", "25", at(17)),
]


# ---------------------------------------------------------------- what the mapping must produce


def _sub(
    n: int,
    customer: int,
    status: SubscriptionStatus,
    recurring: int | None,
    *,
    currency: str = "USD",
    interval: Interval = Interval.MONTH,
    trial_ends: datetime | None = None,
    canceled: datetime | None = None,
) -> Subscription:
    return Subscription(
        source="paddle",
        provider_id=pid("sub", n),
        customer_ref=pid("ctm", customer),
        status=status,
        cycle=BillingCycle(interval=interval, frequency=1),
        currency=currency,
        recurring_minor=recurring,
        started_at=when(1),
        canceled_at=canceled,
        trial_ends_at=trial_ends,
        vendor_updated_at=when(n),
    )


EXPECTED_SUBSCRIPTIONS = (
    _sub(1, 1, SubscriptionStatus.ACTIVE, 500),
    _sub(2, 2, SubscriptionStatus.ACTIVE, 4800, interval=Interval.YEAR),
    _sub(3, 3, SubscriptionStatus.TRIALING, 500, trial_ends=when(30)),
    _sub(4, 4, SubscriptionStatus.CANCELED, 500, canceled=when(4)),
    _sub(5, 1, SubscriptionStatus.PAST_DUE, 400),
    _sub(6, 5, SubscriptionStatus.ACTIVE, 300, currency="GBP"),
    _sub(7, 6, SubscriptionStatus.PAUSED, 500),
)


def _pay(
    n: int,
    txn_n: int,
    sub_n: int | None,
    status: PaymentStatus,
    cents: int,
    tax: int,
    moment: datetime,
    error: str | None = None,
    currency: str = "USD",
) -> Payment:
    return Payment(
        source="paddle",
        provider_id=f"0000000{n}-aaaa-4bbb-8ccc-00000000000{n}",
        transaction_ref=pid("txn", txn_n),
        subscription_ref=None if sub_n is None else pid("sub", sub_n),
        customer_ref=pid("ctm", 5 if currency == "GBP" else (2 if txn_n == 2 else 1)),
        status=status,
        currency=currency,
        amount_minor=cents,
        tax_minor=tax,
        error_code=error,
        occurred_at=moment,
    )


EXPECTED_PAYMENTS = (
    _pay(1, 1, 1, PaymentStatus.FAILED, 525, 25, when(10), "declined"),
    _pay(2, 1, 1, PaymentStatus.SUCCEEDED, 525, 25, when(11, 10)),
    _pay(3, 2, 2, PaymentStatus.SUCCEEDED, 5040, 240, when(5)),
    _pay(4, 3, 5, PaymentStatus.FAILED, 420, 20, when(15), "insufficient_funds"),
    _pay(5, 5, 6, PaymentStatus.SUCCEEDED, 360, 60, when(12), currency="GBP"),
)


def _refund(
    n: int, kind: RefundKind, status: RefundStatus, txn_n: int, cents: int, tax: int, moment: datetime
) -> Refund:
    return Refund(
        source="paddle",
        provider_id=pid("adj", n),
        kind=kind,
        status=status,
        transaction_ref=pid("txn", txn_n),
        subscription_ref=pid("sub", 1),
        currency="USD",
        amount_minor=cents,
        tax_minor=tax,
        created_at=moment,
        vendor_updated_at=moment,
    )


EXPECTED_REFUNDS = (
    _refund(1, RefundKind.REFUND, RefundStatus.APPROVED, 2, 5040, 240, when(6)),
    _refund(3, RefundKind.CHARGEBACK, RefundStatus.APPROVED, 1, 525, 25, when(13)),
    _refund(4, RefundKind.CHARGEBACK_REVERSAL, RefundStatus.APPROVED, 1, 525, 25, when(16)),
    _refund(5, RefundKind.REFUND, RefundStatus.PENDING, 1, 525, 25, when(17)),
)


# ---------------------------------------------------------------- a pretend Paddle API


class PaddleWorld:
    """Answers GET list requests like Paddle; records every request it saw."""

    def __init__(
        self,
        subscriptions: list[dict[str, object]] | None = None,
        transactions: list[dict[str, object]] | None = None,
        adjustments: list[dict[str, object]] | None = None,
        discounts: list[dict[str, object]] | None = None,
    ) -> None:
        self.tables: dict[str, list[dict[str, object]]] = {
            "/subscriptions": SUBSCRIPTIONS if subscriptions is None else subscriptions,
            "/transactions": TRANSACTIONS if transactions is None else transactions,
            "/adjustments": ADJUSTMENTS if adjustments is None else adjustments,
            "/discounts": DISCOUNTS if discounts is None else discounts,
        }
        self.requests: list[httpx.Request] = []

    def serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET" or request.url.path not in self.tables:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        rows = sorted(self.tables[request.url.path], key=lambda r: str(r["id"]))
        if "id" in query:
            wanted = set(query["id"].split(","))
            rows = [r for r in rows if r["id"] in wanted]
        if "updated_at[GTE]" in query:
            since = datetime.fromisoformat(query["updated_at[GTE]"])
            rows = [r for r in rows if datetime.fromisoformat(str(r["updated_at"])) >= since]
        if "after" in query:
            rows = [r for r in rows if str(r["id"]) > query["after"]]
        page, more = rows[:PAGE_SIZE], len(rows) > PAGE_SIZE
        next_url = (
            f"https://sandbox-api.paddle.com{request.url.path}?after={page[-1]['id']}" if page else None
        )
        return httpx.Response(
            200,
            json={
                "data": page,
                "meta": {
                    "request_id": "00000000-0000-4000-8000-000000000000",
                    "pagination": {
                        "per_page": PAGE_SIZE,
                        "next": next_url,
                        "has_more": more,
                        "estimated_total": len(rows),
                    },
                },
            },
        )
