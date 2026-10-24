"""Registry switches and requests (ADR-030): off is always storable; on only through an approved request.

The database's own rules are tested here directly, as the app role, so they hold
whatever the code above the store does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.integration.conftest import ScratchDb, connect_as, open_db, run
from yaqzan.core.store import RegistrySwitchRows, RequestRow, RequestStatus, SwitchRow

TENANT = uuid4()
OTHER = uuid4()
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
FILES = "a" * 64
CHANGED = "b" * 64
CHAIRMAN = "user:chairman"


def request(item: str = "triage-failing-build", **extra: Any) -> RequestRow:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "kind": "skill",
        "item_id": item,
        "fingerprint": FILES,
        "status": RequestStatus.PENDING,
        "requested_by": CHAIRMAN,
        "requested_at": NOW,
    }
    return RequestRow(**{**fields, **extra})


def test_switch_off_is_stored_and_overwritten(test_db: ScratchDb) -> None:
    async def body() -> tuple[list[SwitchRow], list[SwitchRow]]:
        async with open_db(test_db.app_url) as db:
            rows = RegistrySwitchRows(db.scoped(TENANT))
            await rows.switch_off("skill", "a-skill", FILES, changed_by=CHAIRMAN, changed_at=NOW)
            later = NOW + timedelta(minutes=1)
            await rows.switch_off("skill", "a-skill", CHANGED, changed_by="system:yaqzan", changed_at=later)
            return await rows.switches(), await RegistrySwitchRows(db.scoped(OTHER)).switches()

    mine, theirs = run(body())
    assert theirs == []
    (row,) = mine
    assert (row.kind, row.item_id, row.enabled, row.fingerprint, row.request_id) == (
        "skill", "a-skill", False, CHANGED, None,
    )  # fmt: skip
    assert row.changed_by == "system:yaqzan"


def test_one_pending_request_per_item(test_db: ScratchDb) -> None:
    async def body() -> tuple[bool, bool, bool, list[RequestRow]]:
        async with open_db(test_db.app_url) as db:
            rows = RegistrySwitchRows(db.scoped(TENANT))
            first = await rows.add_request(request())
            second = await rows.add_request(request())
            other_item = await rows.add_request(request("another"))
            return first, second, other_item, await rows.pending()

    first, second, other_item, pending = run(body())
    assert (first, second, other_item) == (True, False, True)
    assert sorted(p.item_id for p in pending) == ["another", "triage-failing-build"]
    assert all(p.status is RequestStatus.PENDING for p in pending)


def test_withdraw_once_then_a_new_request_may_be_filed(test_db: ScratchDb) -> None:
    filed = request()

    async def body() -> tuple[bool, bool, bool, RequestRow | None, list[RequestRow]]:
        async with open_db(test_db.app_url) as db:
            rows = RegistrySwitchRows(db.scoped(TENANT))
            await rows.add_request(filed)
            first = await rows.withdraw(filed.id, by=CHAIRMAN, at=NOW)
            again = await rows.withdraw(filed.id, by=CHAIRMAN, at=NOW)
            refiled = await rows.add_request(request())
            return first, again, refiled, await rows.get_request(filed.id), await rows.pending()

    first, again, refiled, stored, pending = run(body())
    assert (first, again, refiled) == (True, False, True)
    assert stored is not None
    assert (stored.status, stored.decided_by, stored.decided_at) == (RequestStatus.WITHDRAWN, CHAIRMAN, NOW)
    assert len(pending) == 1


def test_only_a_pending_request_can_be_filed(test_db: ScratchDb) -> None:
    async def body() -> list[RequestRow]:
        async with open_db(test_db.app_url) as db:
            rows = RegistrySwitchRows(db.scoped(TENANT))
            with pytest.raises(ValueError, match="pending"):
                await rows.add_request(request(status=RequestStatus.APPROVED))
            return await rows.pending()

    assert run(body()) == []


def test_requests_are_scoped_by_tenant(test_db: ScratchDb) -> None:
    filed = request()

    async def body() -> tuple[RequestRow | None, bool]:
        async with open_db(test_db.app_url) as db:
            rows = RegistrySwitchRows(db.scoped(TENANT))
            await rows.add_request(filed)
            elsewhere = RegistrySwitchRows(db.scoped(OTHER))
            found = await elsewhere.get_request(filed.id)
            return found, await elsewhere.withdraw(filed.id, by=CHAIRMAN, at=NOW)

    assert run(body()) == (None, False)


# ---------------------------------------------------------------- the database's own rules (as the app role)

_ON = """INSERT INTO yaqzan.registry_switches (tenant_id, kind, item_id, enabled, fingerprint, request_id,
    changed_by, changed_at) VALUES ($1, 'skill', $2, $5, $3, $4, 'user:chairman', now())"""
_APPROVED = """INSERT INTO yaqzan.registry_requests (id, tenant_id, kind, item_id, fingerprint, status,
    requested_by, requested_at, decided_by, decided_at, step_up_at)
    VALUES ($1, $2, 'skill', $3, $4, 'approved', 'user:chairman', now(), $5, now(), $6)"""


async def _app(test_db: ScratchDb, sql: str, *args: object) -> None:
    conn = await connect_as(test_db.app_url, TENANT)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


def _refused(test_db: ScratchDb, sql: str, *args: object) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        run(_app(test_db, sql, *args))


def _approved(test_db: ScratchDb, item: str) -> UUID:
    approved = uuid4()
    run(_app(test_db, _APPROVED, approved, TENANT, item, FILES, CHAIRMAN, NOW))
    return approved


def _file(test_db: ScratchDb, filed: RequestRow) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await RegistrySwitchRows(db.scoped(TENANT)).add_request(filed)

    run(body())


def test_on_without_a_request_is_refused(test_db: ScratchDb) -> None:
    _refused(test_db, _ON, TENANT, "a-skill", FILES, None, True)


def test_on_with_a_pending_request_is_refused(test_db: ScratchDb) -> None:
    filed = request("a-skill")
    _file(test_db, filed)
    _refused(test_db, _ON, TENANT, "a-skill", FILES, filed.id, True)


def test_on_with_an_approval_for_other_files_or_another_item_is_refused(test_db: ScratchDb) -> None:
    approved = _approved(test_db, "a-skill")
    _refused(test_db, _ON, TENANT, "a-skill", CHANGED, approved, True)
    _refused(test_db, _ON, TENANT, "b-skill", FILES, approved, True)


def test_on_with_a_matching_approval_is_stored(test_db: ScratchDb) -> None:
    approved = _approved(test_db, "a-skill")
    run(_app(test_db, _ON, TENANT, "a-skill", FILES, approved, True))

    async def body() -> list[SwitchRow]:
        async with open_db(test_db.app_url) as db:
            return await RegistrySwitchRows(db.scoped(TENANT)).switches()

    (row,) = run(body())
    assert (row.enabled, row.request_id) == (True, approved)


def test_off_may_not_name_a_request(test_db: ScratchDb) -> None:
    approved = _approved(test_db, "a-skill")
    _refused(test_db, _ON, TENANT, "a-skill", FILES, approved, False)


def test_an_approval_needs_a_person_and_a_step_up(test_db: ScratchDb) -> None:
    _refused(test_db, _APPROVED, uuid4(), TENANT, "a-skill", FILES, CHAIRMAN, None)
    _refused(test_db, _APPROVED, uuid4(), TENANT, "a-skill", FILES, "system:yaqzan", NOW)


def test_a_decided_request_never_changes(test_db: ScratchDb) -> None:
    approved = _approved(test_db, "a-skill")
    reopen = (
        "UPDATE yaqzan.registry_requests SET status = 'pending', decided_by = NULL, decided_at = NULL"
        " WHERE id = $1"
    )
    _refused(test_db, reopen, approved)
    _refused(test_db, "UPDATE yaqzan.registry_requests SET status = 'refused' WHERE id = $1", approved)


def test_what_was_asked_for_cannot_be_rewritten(test_db: ScratchDb) -> None:
    filed = request("a-skill")
    _file(test_db, filed)
    _refused(test_db, "UPDATE yaqzan.registry_requests SET fingerprint = $2 WHERE id = $1", filed.id, CHANGED)
    _refused(test_db, "UPDATE yaqzan.registry_requests SET item_id = 'other' WHERE id = $1", filed.id)


@pytest.mark.parametrize("table", ["registry_switches", "registry_requests"])
def test_the_app_cannot_delete_switches_or_requests(test_db: ScratchDb, table: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(_app(test_db, f"DELETE FROM yaqzan.{table}"))  # noqa: S608  # nosec B608  # fixed table names
