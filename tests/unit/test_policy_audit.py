"""The hash-chained audit log: any edit, deletion, re-order or truncation is caught."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from yaqzan.core.policy import (
    GENESIS_HASH,
    AuditDraft,
    AuditEntry,
    AuditOutcome,
    ChainAnchor,
    append,
    verify_chain,
)

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def draft(n: int = 0, **overrides: Any) -> AuditDraft:
    fields: dict[str, Any] = {
        "at": NOW + timedelta(seconds=n),
        "actor": "agent:support",
        "action": "tool.call",
        "outcome": AuditOutcome.ALLOW,
        "reason_code": "gate.allowed",
        "correlation_id": uuid4(),
        "target_ref": f"ticket:{n}",
        "details": {"tool": "support.list_tickets", "n": n},
    }
    fields.update(overrides)
    return AuditDraft.model_validate(fields)


def chain(length: int) -> list[AuditEntry]:
    entries: list[AuditEntry] = []
    head: AuditEntry | None = None
    for n in range(length):
        head = append(head, draft(n))
        entries.append(head)
    return entries


def test_first_entry_links_to_genesis() -> None:
    first = append(None, draft())
    assert first.seq == 1
    assert first.prev_hash == GENESIS_HASH
    assert len(first.hash) == 64


def test_intact_chain_verifies() -> None:
    entries = chain(5)
    report = verify_chain(entries)
    assert report.ok
    assert report.checked == 5
    assert report.head == ChainAnchor(seq=5, hash=entries[-1].hash)


def test_empty_chain_is_fine_without_anchor() -> None:
    report = verify_chain([])
    assert report.ok
    assert report.head.hash == GENESIS_HASH


def test_edited_entry_is_caught() -> None:
    entries = chain(4)
    entries[2] = entries[2].model_copy(update={"outcome": AuditOutcome.DENY})
    report = verify_chain(entries)
    assert not report.ok
    assert report.bad_seq == 3
    assert report.problem is not None
    assert "content" in report.problem


def test_edited_entry_with_recomputed_hash_breaks_the_next_link() -> None:
    entries = chain(4)
    forged = append(entries[1], draft(99, outcome=AuditOutcome.DENY))
    entries[2] = forged
    report = verify_chain(entries)
    assert not report.ok
    assert report.bad_seq == 4


def test_deleted_entry_is_caught() -> None:
    entries = chain(4)
    del entries[1]
    report = verify_chain(entries)
    assert not report.ok
    assert report.bad_seq == 2


def test_reordered_entries_are_caught() -> None:
    entries = chain(4)
    entries[1], entries[2] = entries[2], entries[1]
    assert not verify_chain(entries).ok


def test_prev_hash_swap_is_caught() -> None:
    entries = chain(3)
    entries[1] = entries[1].model_copy(update={"prev_hash": "f" * 64})
    report = verify_chain(entries)
    assert not report.ok
    assert report.problem is not None
    assert "prev_hash" in report.problem


def test_truncated_tail_is_caught_by_the_anchor() -> None:
    entries = chain(5)
    anchor = ChainAnchor(seq=5, hash=entries[-1].hash)
    report = verify_chain(entries[:3], anchor=anchor)
    assert not report.ok
    assert report.bad_seq == 5
    assert verify_chain(entries, anchor=anchor).ok


def test_fully_rewritten_chain_is_caught_by_the_anchor() -> None:
    real = chain(3)
    anchor = ChainAnchor(seq=3, hash=real[-1].hash)
    rewritten = chain(3)  # self-consistent, but not the chain we anchored
    report = verify_chain(rewritten, anchor=anchor)
    assert not report.ok
    assert report.problem is not None
    assert "anchor" in report.problem


def test_anchor_at_genesis() -> None:
    assert verify_chain([], anchor=ChainAnchor(seq=0, hash=GENESIS_HASH)).ok
    assert not verify_chain([], anchor=ChainAnchor(seq=0, hash="a" * 64)).ok


def test_chain_growing_past_the_anchor_is_fine() -> None:
    entries = chain(6)
    assert verify_chain(entries, anchor=ChainAnchor(seq=2, hash=entries[1].hash)).ok


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_ref": "customer:jane.doe@example.com"},
        {"details": {"note": "card 4111 1111 1111 1111"}},
        {"details": {"note": "x" * 201}},
        {"details": {"jane.doe@example.com": 1}},
        {"details": {"Bad Key": 1}},
        {"actor": "claude"},
        {"action": "Tool Call"},
        {"at": datetime(2026, 10, 17, 9, 0)},  # noqa: DTZ001  # proving naive times are refused
    ],
)
def test_entries_hold_references_never_personal_data(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        draft(**overrides)


def test_entries_are_immutable() -> None:
    entry = append(None, draft())
    with pytest.raises(ValidationError):
        entry.outcome = AuditOutcome.DENY  # type: ignore[misc]  # proving the model is frozen


@settings(max_examples=30, deadline=None)
@given(length=st.integers(min_value=1, max_value=12), data=st.data())
def test_any_single_edit_anywhere_is_caught(length: int, data: st.DataObject) -> None:
    entries = chain(length)
    index = data.draw(st.integers(min_value=0, max_value=length - 1))
    entries[index] = entries[index].model_copy(update={"reason_code": "gate.tampered"})
    report = verify_chain(entries)
    assert not report.ok
    assert report.bad_seq == index + 1


def test_time_zone_of_the_caller_never_changes_the_hash() -> None:
    from datetime import timezone  # noqa: PLC0415  # only this test needs it

    dubai = timezone(timedelta(hours=4))
    same = uuid4()
    entry = append(None, draft(at=NOW.astimezone(dubai), correlation_id=same))
    assert entry.at.utcoffset() == timedelta(0)
    assert entry.hash == append(None, draft(at=NOW, correlation_id=same)).hash
    # The store hands the row back with the time in UTC: the chain must still verify.
    reloaded = AuditEntry.model_validate_json(entry.model_dump_json())
    assert verify_chain([reloaded]).ok
