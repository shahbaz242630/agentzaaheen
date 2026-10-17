"""The stored audit chain: verifies after save and reload, and any tampering is caught (SP-9)."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.integration.conftest import ScratchDb, open_db, run
from yaqzan.core.policy import (
    AuditDraft,
    AuditOutcome,
    ChainAnchor,
    ChainReport,
    KillSwitches,
    Ledger,
    load_kill_switches,
)
from yaqzan.core.store import AuditRow, AuditRows, KillSwitchRows, StoreError, StoreErrorCode, SwitchScope
from yaqzan.core.store import audit as store_audit

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
TENANT = UUID("00000000-0000-4000-8000-000000000001")


def draft(n: int) -> AuditDraft:
    return AuditDraft(
        at=NOW + timedelta(seconds=n),
        actor="agent:support",
        action="tool.call",
        outcome=AuditOutcome.ALLOW,
        reason_code="gate.allowed",
        correlation_id=uuid4(),
        project_id=uuid4(),
        target_ref=f"ticket:{n}",
        details={"tool": "support.list_tickets", "n": n, "tainted": True, "note": None},
    )


async def _fill(url: str, count: int, tenant: UUID = TENANT) -> None:
    async with open_db(url) as db:
        ledger = Ledger(db, tenant)
        for n in range(count):
            await ledger.record(draft(n))


async def _verify(url: str, anchor: ChainAnchor | None = None, tenant: UUID = TENANT) -> ChainReport:
    async with open_db(url) as db:
        return await Ledger(db, tenant).verify(anchor)


async def _as_superuser_without_triggers(url: str, statement: str) -> None:
    """What an attacker with full database access could do: bypass the append-only triggers."""
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(statement)
    finally:
        await conn.close()


def test_chain_verifies_after_save_and_reload(test_db: ScratchDb) -> None:
    run(_fill(test_db.app_url, 5))
    report = run(_verify(test_db.app_url))  # a fresh pool: everything comes back from disk
    assert report.ok
    assert report.checked == 5
    assert report.head.seq == 5


def test_long_chain_is_read_back_page_by_page(test_db: ScratchDb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_audit, "PAGE_SIZE", 2)
    run(_fill(test_db.app_url, 5))
    report = run(_verify(test_db.app_url))
    assert report.ok
    assert report.checked == 5


def test_empty_chain_verifies(test_db: ScratchDb) -> None:
    report = run(_verify(test_db.app_url))
    assert report.ok
    assert report.checked == 0


def test_record_returns_what_was_stored(test_db: ScratchDb) -> None:
    async def body() -> tuple[int, AuditRow | None, str]:
        async with open_db(test_db.app_url) as db:
            entry = await Ledger(db, TENANT).record(draft(0))
            return entry.seq, await AuditRows(db).head(TENANT), entry.hash

    seq, head, digest = run(body())
    assert seq == 1
    assert head is not None
    assert head.hash == digest


def test_concurrent_writers_never_fork_the_chain(test_db: ScratchDb) -> None:
    async def body() -> ChainReport:
        async with open_db(test_db.app_url, max_size=10) as db:
            ledger = Ledger(db, TENANT)
            await asyncio.gather(*(ledger.record(draft(n)) for n in range(25)))
            return await ledger.verify()

    report = run(body())
    assert report.ok
    assert report.checked == 25


def test_chains_are_kept_per_tenant(test_db: ScratchDb) -> None:
    other = uuid4()
    run(_fill(test_db.app_url, 3))
    run(_fill(test_db.app_url, 2, tenant=other))
    assert run(_verify(test_db.app_url)).checked == 3
    assert run(_verify(test_db.app_url, tenant=other)).checked == 2


def test_edited_row_is_caught(test_db: ScratchDb) -> None:
    run(_fill(test_db.app_url, 5))
    run(
        _as_superuser_without_triggers(
            test_db.admin_url,
            "UPDATE yaqzan.audit_log SET reason_code = 'gate.tool_not_allowed' WHERE seq = 3",
        )
    )
    report = run(_verify(test_db.app_url))
    assert not report.ok
    assert report.bad_seq == 3


def test_deleted_row_is_caught(test_db: ScratchDb) -> None:
    run(_fill(test_db.app_url, 5))
    run(_as_superuser_without_triggers(test_db.admin_url, "DELETE FROM yaqzan.audit_log WHERE seq = 2"))
    report = run(_verify(test_db.app_url))
    assert not report.ok
    assert report.bad_seq == 2


def test_cut_off_tail_is_caught_by_the_anchor(test_db: ScratchDb) -> None:
    run(_fill(test_db.app_url, 5))
    anchor = run(_verify(test_db.app_url)).head
    run(_as_superuser_without_triggers(test_db.admin_url, "DELETE FROM yaqzan.audit_log WHERE seq >= 4"))
    assert run(_verify(test_db.app_url)).ok  # without the outside copy, a clean cut looks fine
    report = run(_verify(test_db.app_url, anchor))
    assert not report.ok
    assert "anchor" in (report.problem or "")


def test_row_that_is_no_longer_a_valid_entry_is_reported_not_raised(test_db: ScratchDb) -> None:
    run(_fill(test_db.app_url, 3))
    run(
        _as_superuser_without_triggers(
            test_db.admin_url, "UPDATE yaqzan.audit_log SET actor = 'nobody' WHERE seq = 2"
        )
    )
    report = run(_verify(test_db.app_url))
    assert not report.ok
    assert report.bad_seq == 2
    assert report.checked == 1
    assert report.head.seq == 1


def test_first_row_invalid_reports_genesis_head(test_db: ScratchDb) -> None:
    run(_fill(test_db.app_url, 1))
    run(
        _as_superuser_without_triggers(
            test_db.admin_url, "UPDATE yaqzan.audit_log SET actor = 'x' WHERE seq = 1"
        )
    )
    report = run(_verify(test_db.app_url))
    assert not report.ok
    assert report.bad_seq == 1
    assert report.head.seq == 0


def _relinked(head: AuditRow, *, seq: int, prev_hash: str, tenant_id: UUID | None = None) -> AuditRow:
    fields = {**head.__dict__, "seq": seq, "prev_hash": prev_hash}
    if tenant_id is not None:
        fields["tenant_id"] = tenant_id
    return AuditRow(**fields)


def test_database_refuses_a_row_that_does_not_link(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            head = await Ledger(db, TENANT).record(draft(0))
            stored = await AuditRows(db).head(TENANT)
            assert stored is not None
            forged = _relinked(stored, seq=head.seq + 1, prev_hash="f" * 64)
            await AuditRows(db).append(TENANT, lambda _head: forged)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_database_refuses_a_row_that_skips_ahead(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await Ledger(db, TENANT).record(draft(0))
            stored = await AuditRows(db).head(TENANT)
            assert stored is not None
            forged = _relinked(stored, seq=5, prev_hash=stored.hash)
            await AuditRows(db).append(TENANT, lambda _head: forged)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_database_refuses_a_first_row_without_genesis(test_db: ScratchDb) -> None:
    other = uuid4()

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await Ledger(db, TENANT).record(draft(0))
            stored = await AuditRows(db).head(TENANT)
            assert stored is not None
            forged = _relinked(stored, seq=1, prev_hash="f" * 64, tenant_id=other)
            await AuditRows(db).append(other, lambda _head: forged)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_row_for_another_tenant_cannot_be_slipped_into_a_locked_chain(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await Ledger(db, TENANT).record(draft(0))
            row = await AuditRows(db).head(TENANT)
            assert row is not None
            await AuditRows(db).append(uuid4(), lambda _head: row)

    with pytest.raises(ValueError, match="tenant"):
        run(body())


def test_kill_switches_default_off(test_db: ScratchDb) -> None:
    async def body() -> KillSwitches:
        async with open_db(test_db.app_url) as db:
            return await load_kill_switches(db, TENANT)

    assert run(body()) == KillSwitches()


def test_kill_switches_load_what_was_set(test_db: ScratchDb) -> None:
    project = uuid4()

    async def body() -> tuple[KillSwitches, KillSwitches]:
        async with open_db(test_db.app_url) as db:
            rows = KillSwitchRows(db)
            by = "user:chairman"
            await rows.set(
                TENANT, SwitchScope.PROJECT, str(project), engaged=True, changed_by=by, changed_at=NOW
            )
            await rows.set(TENANT, SwitchScope.AGENT, "support", engaged=True, changed_by=by, changed_at=NOW)
            await rows.set(TENANT, SwitchScope.AGENT, "growth", engaged=False, changed_by=by, changed_at=NOW)
            first = await load_kill_switches(db, TENANT)
            await rows.set(TENANT, SwitchScope.ALL, "", engaged=True, changed_by=by, changed_at=NOW)
            await rows.set(TENANT, SwitchScope.AGENT, "support", engaged=False, changed_by=by, changed_at=NOW)
            return first, await load_kill_switches(db, TENANT)

    first, second = run(body())
    assert first == KillSwitches(paused_agents=frozenset({"support"}), frozen_projects=frozenset({project}))
    assert second.stop_all
    assert second.paused_agents == frozenset()


def test_unreadable_kill_switches_mean_stop_everything(test_db: ScratchDb) -> None:
    sink = io.StringIO()

    async def body() -> KillSwitches:
        async with open_db(test_db.app_url) as db:
            pass  # closed on exit
        return await load_kill_switches(db, TENANT, log_sink=sink)

    assert run(body()).stop_all
    line = json.loads(sink.getvalue().splitlines()[-1])
    assert line["reason_code"] == StoreErrorCode.UNAVAILABLE.value
    assert test_db.server.app_password not in sink.getvalue()


def test_nonsense_kill_switch_row_means_stop_everything(test_db: ScratchDb) -> None:
    async def body() -> KillSwitches:
        async with open_db(test_db.app_url) as db:
            await KillSwitchRows(db).set(
                TENANT,
                SwitchScope.PROJECT,
                "not-a-uuid",
                engaged=True,
                changed_by="user:chairman",
                changed_at=NOW,
            )
            return await load_kill_switches(db, TENANT, log_sink=io.StringIO())

    assert run(body()).stop_all


def test_switch_row_with_unknown_scope_means_stop_everything(
    test_db: ScratchDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown_scope(_self: KillSwitchRows, _tenant: UUID) -> list[object]:
        return [SwitchScope("everything")]  # raises ValueError, as a row with a dropped CHECK would

    monkeypatch.setattr(KillSwitchRows, "engaged", unknown_scope)

    async def body() -> KillSwitches:
        async with open_db(test_db.app_url) as db:
            return await load_kill_switches(db, TENANT, log_sink=io.StringIO())

    assert run(body()).stop_all


def test_naive_switch_time_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await KillSwitchRows(db).set(
                TENANT,
                SwitchScope.ALL,
                "",
                engaged=True,
                changed_by="user:chairman",
                changed_at=datetime(2026, 10, 17),  # noqa: DTZ001  # the point of the test
            )

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.NAIVE_TIME
