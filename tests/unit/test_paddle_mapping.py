"""Paddle JSON → SDK shapes: exact amounts, honest "unknown", loud failure on new values."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from agentzaaheen.core.sdk import PaymentStatus, RefundKind, SourceError, SourceErrorCode, SubscriptionStatus
from agentzaaheen.plugins.connectors.paddle import ALLOWED_HOSTS, mapping
from tests.fixtures import paddle_world as world


def one_sub(**changes: Any) -> dict[str, Any]:
    base = world.sub(1, 1, "active", [world.item(world.price(1, "500"))])
    return {**base, **changes}


def recurring(sub: dict[str, Any], discounts: list[dict[str, Any]] | None = None) -> int | None:
    found = {d["id"]: d for d in (discounts or [])}
    return mapping.subscription(sub, found, world.NOW).recurring_minor


# ---------------------------------------------------------------- subscriptions


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("trialing", SubscriptionStatus.TRIALING),
        ("active", SubscriptionStatus.ACTIVE),
        ("past_due", SubscriptionStatus.PAST_DUE),
        ("paused", SubscriptionStatus.PAUSED),
        ("canceled", SubscriptionStatus.CANCELED),
    ],
)
def test_every_paddle_status_maps(raw: str, expected: SubscriptionStatus) -> None:
    assert mapping.subscription(one_sub(status=raw), {}, world.NOW).status is expected


def test_an_unseen_status_fails_loudly() -> None:
    with pytest.raises(SourceError) as caught:
        mapping.subscription(one_sub(status="hibernating"), {}, world.NOW)
    assert caught.value.code is SourceErrorCode.UNKNOWN_VALUE


def test_quantity_multiplies_and_inactive_or_one_off_items_are_ignored() -> None:
    items = [
        world.item(world.price(1, "500"), quantity=3),
        world.item(world.price(2, "900"), status="inactive"),
        {**world.item(world.price(3, "700")), "recurring": False},
    ]
    assert recurring(one_sub(items=items)) == 1500


@pytest.mark.parametrize(
    "item",
    [
        world.item(world.price(1, "500", tax_mode="internal")),
        world.item(world.price(1, "500", tax_mode="account_setting")),
        world.item(world.price(1, "500", tax_mode="location")),
        world.item(world.price(1, "500", currency="EUR")),
        world.item(world.price(1, "500", cycle=world.YEAR)),
    ],
    ids=["tax-inclusive", "account-default-tax", "location-tax", "country-override", "other-cycle"],
)
def test_a_price_we_cannot_make_exact_is_unknown_not_guessed(item: dict[str, Any]) -> None:
    assert recurring(one_sub(items=[item])) is None


def test_trial_end_is_the_latest_item_trial_end() -> None:
    items = [
        world.item(world.price(1, "500"), status="trialing", trial_ends=world.at(20)),
        world.item(world.price(2, "100"), status="trialing", trial_ends=world.at(25)),
    ]
    assert mapping.subscription(one_sub(items=items), {}, world.NOW).trial_ends_at == world.when(25)
    assert mapping.subscription(one_sub(), {}, world.NOW).trial_ends_at is None


# ---------------------------------------------------------------- discounts


def pct(amount: str, **extra: Any) -> dict[str, Any]:
    return {"id": world.pid("dsc", 9), "type": "percentage", "amount": amount, "currency_code": None, **extra}


def window(starts: str | None = None, ends: str | None = None) -> dict[str, Any]:
    return {"id": world.pid("dsc", 9), "starts_at": starts, "ends_at": ends}


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("20", 400), ("12.5", 437), ("0", 500), ("100", 0), ("33.3", 333)],
)
def test_percentage_discounts_round_half_up(amount: str, expected: int) -> None:
    assert recurring(one_sub(discount=window()), [pct(amount)]) == expected


@pytest.mark.parametrize("amount", ["-5", "101", "abc", "NaN", "Infinity"])
def test_impossible_percentages_are_unknown(amount: str) -> None:
    assert recurring(one_sub(discount=window()), [pct(amount)]) is None


def test_flat_discount_never_goes_below_zero_and_needs_the_same_currency() -> None:
    flat = {"id": world.pid("dsc", 9), "type": "flat", "amount": "800", "currency_code": "USD"}
    assert recurring(one_sub(discount=window()), [flat]) == 0
    assert recurring(one_sub(discount=window()), [{**flat, "currency_code": "GBP"}]) is None
    assert recurring(one_sub(discount=window()), [{**flat, "amount": "1.5"}]) is None


def test_flat_per_seat_discount_scales_with_quantity() -> None:
    seat = {"id": world.pid("dsc", 9), "type": "flat_per_seat", "amount": "100", "currency_code": "USD"}
    items = [world.item(world.price(1, "500"), quantity=3)]
    assert recurring(one_sub(items=items, discount=window()), [seat]) == 1200


def test_restricted_discount_only_touches_listed_prices_or_products() -> None:
    items = [world.item(world.price(1, "500")), world.item({**world.price(2, "300"), "product_id": "pro_x"})]
    by_price = pct("50", restrict_to=[world.pid("pri", 2)])
    by_product = pct("50", restrict_to=["pro_x"])
    nothing = pct("50", restrict_to="not-a-list")
    assert recurring(one_sub(items=items, discount=window()), [by_price]) == 650
    assert recurring(one_sub(items=items, discount=window()), [by_product]) == 650
    assert recurring(one_sub(items=items, discount=window()), [nothing]) == 800


def test_discount_outside_its_window_does_not_apply() -> None:
    assert recurring(one_sub(discount=window(starts=world.at(19))), [pct("50")]) == 500
    assert recurring(one_sub(discount=window(ends=world.at(18, 12))), [pct("50")]) == 500
    assert recurring(one_sub(discount=window(ends=world.at(18, 13))), [pct("50")]) == 250


@pytest.mark.parametrize(
    "discount",
    [
        None,
        {"id": world.pid("dsc", 9), "type": "bogo", "amount": "1", "currency_code": "USD"},
        {"id": world.pid("dsc", 9), "type": "flat", "amount": 100, "currency_code": "USD"},
    ],
    ids=["missing", "unknown-type", "amount-not-text"],
)
def test_a_discount_we_cannot_read_makes_the_price_unknown(discount: dict[str, Any] | None) -> None:
    assert recurring(one_sub(discount=window()), [] if discount is None else [discount]) is None


# ---------------------------------------------------------------- payments


def test_payment_attempt_statuses_and_error_codes() -> None:
    attempts = [
        world.attempt(1, "captured", "525", world.at(3), captured=world.at(3, 11)),
        world.attempt(2, "dropped", "525", world.at(3), error="fraud"),
        world.attempt(3, "canceled", "525", world.at(3), error="Weird Code!"),
        world.attempt(4, "authorized", "525", world.at(3), error="x" * 65),
    ]
    out = mapping.payments(
        world.txn(1, "completed", 1, 1, attempts, tax="25", total="525", updated=world.at(3))
    )
    assert [p.status for p in out] == [
        PaymentStatus.SUCCEEDED,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELED,
        PaymentStatus.PENDING,
    ]
    assert [p.error_code for p in out] == [None, "fraud", "unrecognised", "unrecognised"]
    assert out[0].occurred_at == world.when(3, 11)
    assert out[1].occurred_at == world.when(3)


def test_unseen_payment_status_fails_loudly() -> None:
    txn = world.txn(
        1,
        "completed",
        1,
        1,
        [world.attempt(1, "teleported", "1", world.at(3))],
        tax="0",
        total="1",
        updated=world.at(3),
    )
    with pytest.raises(SourceError) as caught:
        mapping.payments(txn)
    assert caught.value.code is SourceErrorCode.UNKNOWN_VALUE


def test_a_transaction_without_payments_maps_to_none() -> None:
    txn = world.txn(4, "draft", None, 1, [], tax="0", total="0", updated=world.at(2))
    assert mapping.payments({**txn, "payments": None}) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "5.25"),
        ("amount", 525),
        ("amount", "-1"),
        ("created_at", "yesterday"),
        ("created_at", "2026-10-03T09:00:00"),
        ("payment_attempt_id", ""),
    ],
)
def test_malformed_payment_fields_are_bad_responses(field: str, value: object) -> None:
    attempt = {**world.attempt(1, "error", "525", world.at(3)), field: value}
    txn = world.txn(1, "completed", 1, 1, [attempt], tax="25", total="525", updated=world.at(3))
    with pytest.raises(SourceError) as caught:
        mapping.payments(txn)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


# ---------------------------------------------------------------- refunds


def test_credits_and_warnings_are_not_refunds() -> None:
    for action in ("credit", "credit_reverse", "chargeback_warning", "chargeback_warning_reverse"):
        assert mapping.refund(world.adjustment(1, action, "approved", 1, "100", "0", world.at(3))) is None


def test_refund_kinds_map() -> None:
    kinds = [
        mapping.refund(world.adjustment(1, a, "approved", 1, "100", "0", world.at(3)))
        for a in ("refund", "chargeback", "chargeback_reverse")
    ]
    assert [k.kind if k else None for k in kinds] == [
        RefundKind.REFUND,
        RefundKind.CHARGEBACK,
        RefundKind.CHARGEBACK_REVERSAL,
    ]


@pytest.mark.parametrize(("action", "status"), [("gift", "approved"), ("refund", "maybe")])
def test_unseen_adjustment_values_fail_loudly(action: str, status: str) -> None:
    with pytest.raises(SourceError) as caught:
        mapping.refund(world.adjustment(1, action, status, 1, "100", "0", world.at(3)))
    assert caught.value.code is SourceErrorCode.UNKNOWN_VALUE


# ---------------------------------------------------------------- field readers


@pytest.mark.parametrize(
    "bad",
    [
        {"billing_cycle": "monthly"},
        {"billing_cycle": {"interval": "fortnight", "frequency": 1}},
        {"billing_cycle": {"interval": "month", "frequency": True}},
        {"items": "none"},
        {"items": ["x"]},
        {"customer_id": None},
        {"items": [{**world.item(world.price(1, "500")), "quantity": -1}]},
    ],
)
def test_malformed_subscriptions_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(SourceError):
        mapping.subscription(one_sub(**bad), {}, world.NOW)


# ---------------------------------------------------------------- the manifest matches the code


def test_manifest_declares_read_only_and_exactly_our_hosts() -> None:
    path = pathlib.Path(mapping.__file__).with_name("plugin.yaml")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert manifest["id"] == mapping.SOURCE
    assert manifest["type"] == "connector"
    assert manifest["permissions"] == ["read"]
    assert set(manifest["egress"]) == ALLOWED_HOSTS
    assert all(endpoint.startswith("GET /") for endpoint in manifest["endpoints"])


def test_unicode_digits_are_not_amounts() -> None:
    attempt = {**world.attempt(1, "error", "525", world.at(3)), "amount": "5²5"}
    txn = world.txn(1, "completed", 1, 1, [attempt], tax="25", total="525", updated=world.at(3))
    with pytest.raises(SourceError) as caught:
        mapping.payments(txn)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE
    flat = {"id": world.pid("dsc", 9), "type": "flat", "amount": "1²", "currency_code": "USD"}
    assert recurring(one_sub(discount=window()), [flat]) is None


def test_a_percentage_over_several_lines_is_exact_or_unknown() -> None:
    items = [world.item(world.price(1, "333")), world.item(world.price(2, "333"))]
    assert recurring(one_sub(items=items, discount=window()), [pct("50")]) == 333
    assert (
        recurring(one_sub(items=items, discount=window()), [pct("10")]) is None
    )  # 66.6 off: per-line rounding unknown
