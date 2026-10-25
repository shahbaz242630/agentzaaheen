"""The customer-data check before any cloud call (ADR-023, ADR-033): refuse, never trim.

A prompt for a cloud model is built from parts, each tagged with its data
class. Four nets, in order, and any one refuses the whole call:

1. **The tag.** One part tagged customer data refuses it. Records that hold
   customer references (subscriptions, payments, refunds) are tagged customer
   data the moment they become a part (:func:`part_from`), whatever the
   caller thinks; a finding keeps its own tag.
2. **Our own customers' names** (the Chairman's point, 25 Oct 2026: a name
   cannot be found by pattern). Every name we hold is matched as whole words,
   ignoring accents. A full name matches in any case. A one-word name
   ("Grace", "Mark") matches only when written like a name, with a capital
   (or in a script with no capitals, such as Arabic), so "a 7-day grace
   period" is not refused; "Grace periods..." at the start of a sentence
   still is, on the safe side. Without a name list, no cloud call may go.
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
    KNOWN_NAME = "known_customer_name"  # a full name, two words or more
    KNOWN_SINGLE_NAME = "known_customer_single_name"  # a one-word name, written with a capital
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
    """A record as a prompt part, tagged at the source: customer references make it customer data."""
    if isinstance(record, Finding):
        return PromptPart(f"{record.title}\n{record.body}", record.data_class)
    return PromptPart(record.model_dump_json(), DataClass.CUSTOMER_PII)


class KnownNames:
    """Our own customers' names, each kept as its words (case and accents ignored when stored)."""

    def __init__(self, names: Iterable[str]) -> None:
        kept = {
            words
            for name in names
            if (words := _words(name))
            and len(words) <= MAX_NAME_WORDS
            and len("".join(words)) >= MIN_NAME_CHARS
        }
        self._full = frozenset(words for words in kept if len(words) > 1)
        self._single = frozenset(words[0] for words in kept if len(words) == 1)
        self._first = frozenset(words[0] for words in self._full)
        self._longest = max((len(words) for words in self._full), default=0)

    def __len__(self) -> int:
        return len(self._full) + len(self._single)

    def match_in(self, text: str) -> Found | None:
        """Which kind of our customers' names ``text`` holds, if any."""
        tokens = _tokens(text)
        words = tuple(word for word, _ in tokens)
        for i, (word, written_as_name) in enumerate(tokens):
            if word in self._first and any(
                words[i : i + n] in self._full for n in range(2, self._longest + 1)
            ):
                return Found.KNOWN_NAME
            if written_as_name and word in self._single:
                return Found.KNOWN_SINGLE_NAME
        return None

    def found_in(self, text: str) -> bool:
        """True if ``text`` holds any of the names."""
        return self.match_in(text) is not None


def customer_data_in(parts: Sequence[PromptPart], names: KnownNames | None) -> Found | None:
    """What makes these parts unfit for a cloud model, or ``None`` if they may go."""
    if any(part.data_class is DataClass.CUSTOMER_PII for part in parts):
        return Found.TAGGED
    if sum(len(part.text) for part in parts) > MAX_CHECKED_CHARS:
        return Found.TOO_LONG
    if names is None:
        return Found.NO_NAME_LIST
    for part in parts:
        named = names.match_in(part.text)
        if named is not None:
            return named
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


def _plain(text: str) -> str:
    """``text`` with accents removed ("Zoë" becomes "Zoe"), keeping its case."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _words(text: str) -> tuple[str, ...]:
    """Lower-case words with accents removed, so "Zoë O'Brien" and "zoe o brien" match."""
    return tuple(_LETTERS.findall(_plain(text).casefold()))


def _tokens(text: str) -> list[tuple[str, bool]]:
    """Each word lower-cased, and whether it was written like a name (a capital, or a script without case)."""
    return [
        (word.casefold(), word[0].isupper() or word[0].lower() == word[0].upper())
        for word in _LETTERS.findall(_plain(text))
    ]


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
