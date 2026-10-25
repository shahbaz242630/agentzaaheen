"""The customer-data check before a cloud call: refuse, never trim (F10 slice 1, ADR-023)."""

from __future__ import annotations

import time

import pytest

from yaqzan.core.llm_gateway.cloud_check import MAX_CHECKED_CHARS, Found, PromptPart, customer_data_in
from yaqzan.core.sdk import DataClass

OPS = PromptPart(
    "Zaaheen: 3 builds failed on 2026-10-25 at 07:00; MRR USD 58.50, up 12% (run 4821, PR #52).",
    DataClass.INTERNAL,
)


def test_our_own_ops_text_with_dates_amounts_and_ids_may_go() -> None:
    timestamp = PromptPart("Run started 1761375600, took 1234 ms, cost 0.0412 USD.", DataClass.PUBLIC)
    assert customer_data_in([OPS, timestamp]) is None


def test_a_name_in_a_part_tagged_customer_data_refuses_the_whole_call() -> None:
    planted = PromptPart("Fatima Al Mansoori asked for a refund.", DataClass.CUSTOMER_PII)
    assert customer_data_in([OPS, planted]) is Found.TAGGED


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Reply to fatima.m@example.ae about the refund.", Found.EMAIL),
        ("Customer ctm_01hv8x4k2m9q7r3t5w6y8z0abc churned.", Found.CUSTOMER_ID),
        ("Subscription sub_01hv8x4k2m9q7r3t5w6y8z0abc paused.", Found.CUSTOMER_ID),
        ("Stripe customer cus_Q1w2E3r4T5y6 disputed.", Found.CUSTOMER_ID),
        ("Card 4242 4242 4242 4242 was declined.", Found.CARD),
        ("Call them on +971 50 123 4567 today.", Found.PHONE),
        ("Their mobile is 050-123-4567.", Found.PHONE),
    ],
)
def test_the_second_net_catches_customer_details_in_a_part_tagged_wrongly(text: str, found: Found) -> None:
    assert customer_data_in([OPS, PromptPart(text, DataClass.INTERNAL)]) is found


def test_a_long_number_that_fails_the_card_checksum_is_not_a_card() -> None:
    assert customer_data_in([PromptPart("Order 4242 4242 4242 4241 shipped.", DataClass.INTERNAL)]) is None


def test_a_prompt_too_long_to_check_is_refused_unread() -> None:
    big = PromptPart("a" * (MAX_CHECKED_CHARS + 1), DataClass.PUBLIC)
    assert customer_data_in([big]) is Found.TOO_LONG


def test_a_hostile_part_cannot_make_the_check_slow() -> None:
    hostile = PromptPart(("a." * 200_000) + ("1 " * 200_000) + ("+" * 200_000), DataClass.PUBLIC)
    started = time.monotonic()
    assert customer_data_in([hostile]) is None
    assert time.monotonic() - started < 5
