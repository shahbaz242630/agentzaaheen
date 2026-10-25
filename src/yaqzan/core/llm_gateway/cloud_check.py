"""The customer-data check before any cloud call (ADR-023, ADR-033): refuse, never trim.

A prompt for a cloud model is built from parts, each tagged with its data
class. One part tagged customer data refuses the whole call. Then a second
net reads every part for things that look like a customer's details (an
email, a phone number, a card number, a payment provider's customer or
subscription ID) and refuses on any of them, so a part tagged wrongly is
still caught. Names cannot be found by pattern: they are kept out by the tag,
which is why the prompt is built only from parts that carry one.

The answer names only the kind of thing found, never the text itself.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from yaqzan.core.sdk import DataClass

MAX_CHECKED_CHARS = 2_000_000  # far above any prompt we send; anything longer is refused unread


class Found(StrEnum):
    """Why a prompt was refused. Safe to log: a kind, never the matched text."""

    TAGGED = "tagged_customer_data"
    EMAIL = "email"
    PHONE = "phone"
    CARD = "card"
    CUSTOMER_ID = "customer_id"
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class PromptPart:
    """One piece of a prompt and how sensitive it is."""

    text: str
    data_class: DataClass


# Bounded patterns, so a hostile 2 MB part cannot make the check slow.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}")
_PHONE = re.compile(r"(?<![\w+])(?:\+\d[\d ().-]{7,18}\d|0\d{2}[ -]?\d{3}[ -]?\d{4})(?!\w)")
_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_CUSTOMER_ID = re.compile(
    # Paddle customer, subscription, payment, address and business IDs
    r"\b(?:ctm|sub|txn|add|biz)_[a-z0-9]{20,40}\b"
    r"|\bcus_[A-Za-z0-9]{8,32}\b"  # Stripe customer
    r"|\$RCAnonymousID:[0-9a-f]{16,40}"  # RevenueCat anonymous user
)


def customer_data_in(parts: Sequence[PromptPart]) -> Found | None:
    """What makes these parts unfit for a cloud model, or ``None`` if they may go."""
    if any(part.data_class is DataClass.CUSTOMER_PII for part in parts):
        return Found.TAGGED
    if sum(len(part.text) for part in parts) > MAX_CHECKED_CHARS:
        return Found.TOO_LONG
    for part in parts:
        found = _second_net(part.text)
        if found is not None:
            return found
    return None


def _second_net(text: str) -> Found | None:
    if _EMAIL.search(text):
        return Found.EMAIL
    if _CUSTOMER_ID.search(text):
        return Found.CUSTOMER_ID
    if any(_luhn(match.group()) for match in _CARD.finditer(text)):
        return Found.CARD
    if _PHONE.search(text):
        return Found.PHONE
    return None


def _luhn(number: str) -> bool:
    """True for a digit run that passes the card-number checksum (most random numbers do not)."""
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, digit in enumerate(reversed(digits)):
        doubled = digit * 2 if i % 2 else digit
        total += doubled - 9 if doubled > 9 else doubled  # the checksum's own rule
    return total % 10 == 0
