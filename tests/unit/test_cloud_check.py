"""The customer-data check before a cloud call: refuse, never trim (F10 slices 1 and 2a, ADR-023)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentzaaheen.core.llm_gateway.cloud_check import (
    MAX_CHECKED_CHARS,
    Found,
    KnownNames,
    PrintKind,
    PromptPart,
    customer_data_in,
    part_from,
    prints_of,
)
from agentzaaheen.core.sdk import (
    BillingCycle,
    DataClass,
    Evidence,
    Finding,
    FindingKind,
    Interval,
    Severity,
    Subscription,
    SubscriptionStatus,
    Trust,
)
from agentzaaheen.core.vault import PRINT_BYTES, MasterKey, NamePrinter
from tests.fixtures.name_prints import PRINTER

OPS = PromptPart(
    "Zaaheen: 3 builds failed on 2026-10-25 at 07:00; MRR USD 58.50, up 12% (run 4821, PR #52).",
    DataClass.INTERNAL,
)
NO_NAMES = KnownNames.empty()
CUSTOMERS = KnownNames.of(
    ["Fatima Al Mansoori", "Zoë O'Brien", "Renée Dubois", "محمد علي", "Jo", "Ali", "Grace", "Mark", "سارة"],
    PRINTER,
)


def test_our_own_ops_text_with_dates_amounts_and_ids_may_go() -> None:
    timestamp = PromptPart("Run started 1761375600, took 1234 ms, cost 0.0412 USD.", DataClass.PUBLIC)
    assert customer_data_in([OPS, timestamp], CUSTOMERS) is None


def test_a_name_in_a_part_tagged_customer_data_refuses_the_whole_call() -> None:
    planted = PromptPart("Someone asked for a refund.", DataClass.CUSTOMER_PII)
    assert customer_data_in([OPS, planted], NO_NAMES) is Found.TAGGED


# ---- our own customers' names (slice 2a)


@pytest.mark.parametrize(
    "text",
    [
        "Fatima Al Mansoori asked for a refund.",
        "fatima  al   MANSOORI asked again.",  # case and spacing do not matter
        "Zoe O Brien wrote in.",  # accents and the apostrophe do not matter
        "Renee Dubois wrote in.",  # an accent inside a word does not split it
        "زارنا محمد علي أمس",  # Arabic script
    ],
)
def test_one_of_our_customers_names_refuses_a_part_tagged_wrongly(text: str) -> None:
    assert customer_data_in([OPS, PromptPart(text, DataClass.INTERNAL)], CUSTOMERS) is Found.KNOWN_NAME


@pytest.mark.parametrize(
    "text",
    [
        "Ali renewed.",  # a short one-word name still counts at 3 letters
        "We heard from Grace today.",
        "Mark asked for an invoice.",
        "GRACE wrote in capitals.",
        "زارتنا سارة أمس",  # a script with no capitals: always written like a name
        "Grace periods are 7 days.",  # at the start of a sentence it is still refused, on the safe side
    ],
)
def test_a_one_word_name_written_like_a_name_refuses(text: str) -> None:
    assert customer_data_in([OPS, PromptPart(text, DataClass.INTERNAL)], CUSTOMERS) is Found.KNOWN_SINGLE_NAME


@pytest.mark.parametrize(
    "text",
    [
        "Accounts get a 7-day grace period.",
        "Please mark this finding as read.",
        "The ali-baba test fixture ran.",
    ],
)
def test_a_one_word_name_used_as_an_ordinary_word_may_go(text: str) -> None:
    assert customer_data_in([OPS, PromptPart(text, DataClass.INTERNAL)], CUSTOMERS) is None


def test_a_full_name_is_refused_in_any_case_even_lower_case() -> None:
    lower = PromptPart("fatima al mansoori asked again.", DataClass.INTERNAL)
    assert customer_data_in([lower], CUSTOMERS) is Found.KNOWN_NAME


def test_part_of_a_full_name_alone_is_not_a_match() -> None:
    assert (
        customer_data_in([PromptPart("Renée from support replied.", DataClass.INTERNAL)], CUSTOMERS) is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "Fatima's colleague wrote in.",  # part of a name is not the name
        "The Alias field is empty.",  # a name inside another word is not the name
        "Jo and Jonas reviewed the PR.",  # names under 3 letters are not kept: they would match words
    ],
)
def test_parts_of_names_and_names_inside_words_are_not_matches(text: str) -> None:
    assert customer_data_in([PromptPart(text, DataClass.INTERNAL)], CUSTOMERS) is None


def test_with_no_name_list_no_cloud_call_may_go() -> None:
    assert customer_data_in([OPS], None) is Found.NO_NAME_LIST


def test_an_empty_name_list_is_a_real_answer_not_a_missing_one() -> None:
    assert len(NO_NAMES) == 0
    assert customer_data_in([OPS], NO_NAMES) is None


def test_only_fingerprints_are_kept_never_the_names() -> None:
    prints = prints_of(["Fatima Al Mansoori", "Grace"], PRINTER)
    assert {p.kind for p in prints} == {PrintKind.FULL, PrintKind.FIRST, PrintKind.SINGLE}
    kept = b"".join(p.print for p in prints)
    for word in ("fatima", "mansoori", "grace", "Fatima", "Grace"):
        assert word.encode() not in kept
    assert all(len(p.print) == PRINT_BYTES for p in prints)


def test_prints_made_with_another_key_match_nothing() -> None:
    other = NamePrinter(MasterKey("other-names", bytes(32)))
    stolen = KnownNames(prints_of(["Fatima Al Mansoori", "Grace"], other), PRINTER)
    assert (
        customer_data_in([PromptPart("Fatima Al Mansoori and Grace wrote.", DataClass.INTERNAL)], stolen)
        is None
    )


def test_the_same_name_written_differently_gives_the_same_fingerprint() -> None:
    assert prints_of(["Zoë O'Brien"], PRINTER) == prints_of(["ZOE o brien"], PRINTER)


def test_a_long_name_list_still_checks_quickly() -> None:
    names = KnownNames.of((f"Customer Number{i} Example" for i in range(20_000)), PRINTER)
    text = "ordinary words " * 100_000
    started = time.process_time()  # our own CPU time: other programs on the laptop cannot fail it
    assert not names.found_in(text)
    assert names.found_in("We heard from customer number19999 example today.")
    assert time.process_time() - started < 5


# ---- tagged at the source (slice 2a)


def test_a_subscription_is_customer_data_the_moment_it_becomes_a_part() -> None:
    part = part_from(_subscription())
    assert part.data_class is DataClass.CUSTOMER_PII
    assert customer_data_in([OPS, part], NO_NAMES) is Found.TAGGED
    # its short references would slip past the second net: the tag is what stops it
    assert customer_data_in([PromptPart(part.text, DataClass.INTERNAL)], NO_NAMES) is None


def test_a_finding_keeps_its_own_tag() -> None:
    clean = part_from(_finding(DataClass.INTERNAL))
    assert clean.data_class is DataClass.INTERNAL
    assert customer_data_in([clean], NO_NAMES) is None
    assert part_from(_finding(DataClass.CUSTOMER_PII)).data_class is DataClass.CUSTOMER_PII


# ---- the second net (slice 1)


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
    assert customer_data_in([OPS, PromptPart(text, DataClass.INTERNAL)], NO_NAMES) is found


def test_a_long_number_that_fails_the_card_checksum_is_not_a_card() -> None:
    assert (
        customer_data_in([PromptPart("Order 4242 4242 4242 4241 shipped.", DataClass.INTERNAL)], NO_NAMES)
        is None
    )


def test_a_prompt_too_long_to_check_is_refused_unread() -> None:
    big = PromptPart("a" * (MAX_CHECKED_CHARS + 1), DataClass.PUBLIC)
    assert customer_data_in([big], CUSTOMERS) is Found.TOO_LONG


def test_a_hostile_part_cannot_make_the_check_slow() -> None:
    hostile = PromptPart(("a." * 200_000) + ("1 " * 200_000) + ("+" * 200_000), DataClass.PUBLIC)
    started = time.process_time()  # our own CPU time: other programs on the laptop cannot fail it
    assert customer_data_in([hostile], CUSTOMERS) is None
    assert time.process_time() - started < 5


def _subscription() -> Subscription:
    return Subscription(
        source="paddle",
        provider_id="sub_1",
        customer_ref="ctm_1",
        status=SubscriptionStatus.ACTIVE,
        cycle=BillingCycle(interval=Interval.MONTH, frequency=1),
        currency="USD",
        recurring_minor=500,
        vendor_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _finding(data_class: DataClass) -> Finding:
    return Finding(
        tenant_id=uuid4(),
        project_id=uuid4(),
        kind=FindingKind.INCIDENT,
        severity=Severity.P3,
        title="Builds failing",
        body="3 builds failed this week.",
        evidence=(Evidence(metric_id="builds.failed"),),
        trust=Trust.TRUSTED,
        data_class=data_class,
    )
