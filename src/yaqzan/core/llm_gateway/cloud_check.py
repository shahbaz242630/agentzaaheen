"""The customer-data check before any cloud call (ADR-023, ADR-033): refuse, never trim.

A prompt for a cloud model is built from parts, each tagged with its data
class. Four nets, in order, and any one refuses the whole call:

1. **The tag.** One part tagged customer data refuses it. Records that hold
   customer references (subscriptions, payments, refunds) are tagged customer
   data the moment they become a part (:func:`part_from`), whatever the
   caller thinks; a finding keeps its own tag.
2. **Our own customers' names** (the Chairman's point, 25 Oct 2026: a name
   cannot be found by pattern). Every name we hold is matched as whole words,
   ignoring case and accents. Without a name list, no cloud call may go.
3. **The second net:** things that look like a customer's details (an email,
   a phone number, a card number, a payment provider's customer or
   subscription ID), so a part tagged wrongly is still caught.
4. **Size:** a prompt too long to check is refused unread.

A name we have never stored can still get through; that person is not one
of our customers. The answer names only the kind of thing found, never the text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from yaqzan.core.sdk import DataClass, Finding, Payment, Refund, Subscription

MAX_CHECKED_CHARS = 2_000_000  # far above any prompt we send; anything longer is refused unread
MIN_NAME_CHARS = 3  # shorter "names" ("Al", "Jo") would refuse ordinary words
MAX_NAME_WORDS = 6


class Found(StrEnum):
    """Why a prompt was refused. Safe to log: a kind, never the matched text."""

    TAGGED = "tagged_customer_data"
    NO_NAME_LIST = "no_name_list"
    KNOWN_NAME = "known_customer_name"
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


def part_from(record: Subscription | Payment | Refund | Finding) -> PromptPart:
    """A record as a prompt part, tagged at the source: customer references make it is customer data."""
    if isinstance(record, Finding):
        return PromptPart(f"{record.title}\n{record.body}", record.data_class)
    return PromptPart(record.model_dump_json(), DataClass.CUSTOMER_PII)


class KnownNames:
    """Our own customers' names, each kept as its words (case and accents ignored)."""

    def __init__(self, names: Iterable[str]) -> None:
        kept = {
            words
            for name in names
            if (words := _words(name))
            and len(words) <= MAX_NAME_WORDS
            and len("".join(words)) >= MIN_NAME_CHARS
        }
        self._names = frozenset(kept)
        self._first = frozenset(words[0] for words in kept)
        self._longest = max((len(words) for words in kept), default=0)

    def __len__(self) -> int:
        return len(self._names)

    def found_in(self, text: str) -> bool:
        """True if ``text`` holds any of the names as whole words."""
        words = _words(text)
        for i, word in enumerate(words):
            if word in self._first and any(
                words[i : i + n] in self._names for n in range(1, self._longest + 1)
            ):
                return True
        return False


def customer_data_in(parts: Sequence[PromptPart], names: KnownNames | None) -> Found | None:
    """What makes these parts unfit for a cloud model, or ``None`` if they may go."""
    if any(part.data_class is DataClass.CUSTOMER_PII for part in parts):
        return Found.TAGGED
    if sum(len(part.text) for part in parts) > MAX_CHECKED_CHARS:
        return Found.TOO_LONG
    if names is None:
        return Found.NO_NAME_LIST
    if any(names.found_in(part.text) for part in parts):
        return Found.KNOWN_NAME
    for part in parts:
        found = _second_net(part.text)
        if found is not None:
            return found
    return None


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
_LETTERS = re.compile(r"[^\W\d_]+")  # runs of letters in any script (Latin, Arabic, ...)


def _words(text: str) -> tuple[str, ...]:
    """Lower-case words with accents removed, so "Zoë O'Brien" and "zoe o brien" match."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return tuple(_LETTERS.findall(plain.casefold()))


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
