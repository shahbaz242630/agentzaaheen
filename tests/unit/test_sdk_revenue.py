"""The SDK revenue shapes refuse impossible data (METRICS §2, §5)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agentzaaheen.core.sdk import SDK_VERSION, RevenueBatch, SourceError, SourceErrorCode, SubscriptionStatus
from tests.fixtures import paddle_world as world

PAYMENT = world.EXPECTED_PAYMENTS[0]
REFUND = world.EXPECTED_REFUNDS[0]
SUB = world.EXPECTED_SUBSCRIPTIONS[0]


def test_sdk_version_is_bumped_for_the_new_shapes() -> None:
    assert SDK_VERSION == "1.14.0"


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "usd"},
        {"currency": "US"},
        {"amount_minor": -1},
        {"amount_minor": 1.5},
        {"tax_minor": 600},
        {"provider_id": "has space"},
        {"provider_id": "x" * 129},
        {"error_code": "Not Allowed"},
        {"source": "Paddle"},
        {"occurred_at": "2026-10-03T09:00:00"},
    ],
)
def test_payment_rules(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        type(PAYMENT).model_validate({**PAYMENT.model_dump(), **change})


@pytest.mark.parametrize("change", [{"tax_minor": 6000}, {"amount_minor": -5}, {"kind": "gift"}])
def test_refund_rules(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        type(REFUND).model_validate({**REFUND.model_dump(), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"recurring_minor": -1},
        {"cycle": {"interval": "month", "frequency": 0}},
        {"cycle": {"interval": "fortnight", "frequency": 1}},
        {"vendor_updated_at": None},
        {"extra": 1},
    ],
)
def test_subscription_rules(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        type(SUB).model_validate({**SUB.model_dump(), **change})


def test_shapes_are_frozen() -> None:
    with pytest.raises(ValidationError):
        SUB.status = SubscriptionStatus.CANCELED  # type: ignore[misc]  # proving it is refused


def test_a_batch_refuses_duplicates_and_other_sources() -> None:
    with pytest.raises(ValidationError, match="appears twice"):
        RevenueBatch(source="paddle", payments=(PAYMENT, PAYMENT), fetched_at=world.NOW)
    stripe_refund = REFUND.model_copy(update={"source": "stripe"})
    with pytest.raises(ValidationError, match="another source"):
        RevenueBatch(source="paddle", refunds=(stripe_refund,), fetched_at=world.NOW)


def test_source_error_message_carries_the_code() -> None:
    error = SourceError(SourceErrorCode.AUTH, "refused")
    assert str(error) == "source.auth: refused"
    assert error.code is SourceErrorCode.AUTH
