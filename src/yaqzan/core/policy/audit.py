"""The audit log: an append-only, hash-chained record of every decision (SP-9).

Each entry stores the previous entry's hash, and its own hash is
``SHA-256(prev_hash || canonical_entry)``. Changing, deleting or re-ordering
any entry breaks every hash after it, which :func:`verify_chain` reports.

Rewriting the whole tail consistently is still possible for someone with
database write access, so the head (seq + hash) is also copied outside the
database (heartbeat / Telegram) and passed back in as an ``anchor``.

Entries hold references only, never personal data or secrets (SECURITY §8),
so erasing a customer's data never needs an audit entry to change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from yaqzan.core.sdk import contains_sensitive

GENESIS_HASH = "0" * 64
_SHA256_HEX = r"^[0-9a-f]{64}$"
_MAX_DETAIL_TEXT = 200
_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class AuditOutcome(StrEnum):
    """Whether the audited thing was allowed, denied, or simply happened."""

    ALLOW = "allow"
    DENY = "deny"
    INFO = "info"


DetailValue = str | int | bool | None


class AuditDraft(BaseModel):
    """What the caller supplies. Sequence and hashes are added by :func:`append`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: AwareDatetime
    actor: str = Field(pattern=r"^(agent|user|system|operator):[A-Za-z0-9_.-]{1,64}$")
    action: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=80)
    outcome: AuditOutcome
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,79}$")
    correlation_id: UUID
    project_id: UUID | None = None
    target_ref: str | None = Field(default=None, max_length=256)
    details: dict[str, DetailValue] = Field(default_factory=dict, max_length=20)

    @field_validator("at")
    @classmethod
    def _at_in_utc(cls, value: datetime) -> datetime:
        # One canonical form, so a database that hands times back in UTC never breaks a hash.
        return value.astimezone(UTC)

    @field_validator("target_ref")
    @classmethod
    def _ref_not_sensitive(cls, value: str | None) -> str | None:
        if value is not None and contains_sensitive(value):
            raise ValueError("audit target_ref must be a reference, not personal data or a secret")
        return value

    @field_validator("details")
    @classmethod
    def _details_not_sensitive(cls, value: dict[str, DetailValue]) -> dict[str, DetailValue]:
        for key, item in value.items():
            if not _DETAIL_KEY.match(key):
                raise ValueError("audit detail keys must be short snake_case names")
            if isinstance(item, str) and (len(item) > _MAX_DETAIL_TEXT or contains_sensitive(item)):
                raise ValueError(f"audit detail {key!r} must be a short reference, not content")
        return value


class AuditEntry(AuditDraft):
    """One stored audit row."""

    seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=_SHA256_HEX)
    hash: str = Field(pattern=_SHA256_HEX)


class ChainAnchor(BaseModel):
    """A known-good head of the chain, kept outside the database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    hash: str = Field(pattern=_SHA256_HEX)


class ChainReport(BaseModel):
    """Result of verifying a chain. ``ok`` is False at the first problem found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked: int
    head: ChainAnchor
    problem: str | None = None
    bad_seq: int | None = None


def canonical_bytes(draft: AuditDraft, seq: int, prev_hash: str) -> bytes:
    """The exact bytes that are hashed for one entry."""
    body = draft.model_dump(mode="json", include=set(AuditDraft.model_fields))
    body["seq"] = seq
    body["prev_hash"] = prev_hash
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def entry_hash(draft: AuditDraft, seq: int, prev_hash: str) -> str:
    """``SHA-256(prev_hash || canonical_entry)`` as lowercase hex."""
    return hashlib.sha256(bytes.fromhex(prev_hash) + canonical_bytes(draft, seq, prev_hash)).hexdigest()


def append(head: AuditEntry | None, draft: AuditDraft) -> AuditEntry:
    """Return the entry that follows ``head`` (or the first entry when ``head`` is None)."""
    seq = 1 if head is None else head.seq + 1
    prev_hash = GENESIS_HASH if head is None else head.hash
    fields = draft.model_dump(include=set(AuditDraft.model_fields))
    return AuditEntry(**fields, seq=seq, prev_hash=prev_hash, hash=entry_hash(draft, seq, prev_hash))


def _fail(checked: int, head: ChainAnchor, problem: str, bad_seq: int) -> ChainReport:
    return ChainReport(ok=False, checked=checked, head=head, problem=problem, bad_seq=bad_seq)


def verify_chain(  # noqa: PLR0911  # one return per kind of break
    entries: Iterable[AuditEntry], *, anchor: ChainAnchor | None = None
) -> ChainReport:
    """Check every link of the chain, in order, and that it still contains ``anchor``."""
    head = ChainAnchor(seq=0, hash=GENESIS_HASH)
    if anchor is not None and anchor.seq == 0 and anchor.hash != GENESIS_HASH:
        return _fail(0, head, "anchor at seq 0 must be the genesis hash", 0)
    anchor_seen = anchor is None or anchor.seq == 0
    checked = 0
    for entry in entries:
        expected_seq = head.seq + 1
        if entry.seq != expected_seq:
            return _fail(checked, head, f"expected seq {expected_seq}, found {entry.seq}", expected_seq)
        if entry.prev_hash != head.hash:
            return _fail(checked, head, "prev_hash does not match the previous entry", entry.seq)
        if entry.hash != entry_hash(entry, entry.seq, entry.prev_hash):
            return _fail(checked, head, "entry content does not match its hash", entry.seq)
        checked += 1
        head = ChainAnchor(seq=entry.seq, hash=entry.hash)
        if anchor is not None and entry.seq == anchor.seq:
            if entry.hash != anchor.hash:
                return _fail(checked, head, "chain was rewritten: anchor hash differs", entry.seq)
            anchor_seen = True
    if anchor is not None and not anchor_seen:
        return _fail(checked, head, f"chain ends before anchor seq {anchor.seq}: entries removed", anchor.seq)
    return ChainReport(ok=True, checked=checked, head=head)
