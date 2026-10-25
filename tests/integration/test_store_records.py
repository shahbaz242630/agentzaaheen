"""SDK records survive a round trip through Postgres, once only, and only for their own tenant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentzaaheen.core.sdk import (
    Action,
    ActionLevel,
    ActionStatus,
    DataClass,
    Event,
    Evidence,
    Finding,
    FindingKind,
    ModelInfo,
    RunOutcome,
    RunRecord,
    Severity,
    Trust,
)
from agentzaaheen.core.store import MAX_PAGE, Records, StoreError, StoreErrorCode
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
TENANT = UUID("00000000-0000-4000-8000-000000000001")
PROJECT = UUID("00000000-0000-4000-8000-0000000000aa")


@pytest.fixture(autouse=True)
def _workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT))


def event(n: int = 0, **overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "type": "ticket.created",
        "source": "imap",
        "vendor_event_id": f"msg-{n}",
        "idempotency_key": f"imap:msg-{n}",
        "occurred_at": NOW + timedelta(seconds=n),
        "received_at": NOW + timedelta(seconds=n, milliseconds=250),
        "schema_version": 1,
        "payload": {"subject": "Refund?", "n": n, "nested": {"ok": True, "list": [1, 2.5, None]}},
        "trust": Trust.UNTRUSTED,
        "data_class": DataClass.CUSTOMER_PII,
        "subject_ref": "cust_7f3a",
    }
    fields.update(overrides)
    return Event.model_validate(fields)


def run_record(**overrides: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "agent": "support",
        "agent_version": "1.0.0",
        "trigger_event_ids": (uuid4(), uuid4()),
        "prompt_hash": "a" * 64,
        "model": ModelInfo(
            tier=1, model_id="gemma-4-e4b", quant="q4_k_m", file_sha256="b" * 64, provider="ollama"
        ),
        "data_class_max": DataClass.CUSTOMER_PII,
        "trust": Trust.UNTRUSTED,
        "steps": 4,
        "tokens_in": 1200,
        "tokens_out": 300,
        "cost_minor_units": 0,
        "started_at": NOW,
        "duration_ms": 8123,
        "outcome": RunOutcome.OK,
    }
    fields.update(overrides)
    return RunRecord.model_validate(fields)


def finding(run_id: UUID | None, **overrides: Any) -> Finding:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "run_id": run_id,
        "kind": FindingKind.RISK,
        "severity": Severity.P2,
        "title": "Refund requests doubled this week",
        "body": "Seven refund emails since Monday.",
        "evidence": (
            Evidence(event_id=uuid4()),
            Evidence(url="https://status.example.com"),
            Evidence(metric_id="m1"),
        ),
        "metric_refs": {"refunds_7d": "metric:refunds:7d"},
        "trust": Trust.UNTRUSTED,
        "data_class": DataClass.INTERNAL,
    }
    fields.update(overrides)
    return Finding.model_validate(fields)


def action(run_id: UUID, **overrides: Any) -> Action:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "run_id": run_id,
        "tool": "support.draft_reply",
        "level": ActionLevel.DRAFT,
        "payload": {"to": "cust_7f3a", "body": "Thanks, we are on it."},
        "target_kind": "ticket",
        "target_ref": "ticket:42",
        "data_classes": (DataClass.CUSTOMER_PII, DataClass.INTERNAL),
        "leak_check_passed": True,
        "idempotency_key": "support:evt1:support.draft_reply:ticket:42",
    }
    fields.update(overrides)
    return Action.model_validate(fields)


def test_event_round_trip_is_exact(test_db: ScratchDb) -> None:
    original = event()

    async def body() -> Event | None:
        async with open_db(test_db.app_url) as db:
            assert await Records(db.scoped(TENANT, PROJECT)).add_event(original) is True
            return await Records(db.scoped(TENANT, PROJECT)).get_event(original.id)

    assert run(body()) == original


def test_same_outside_event_is_stored_once(test_db: ScratchDb) -> None:
    async def body() -> tuple[bool, bool, int]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            first = await records.add_event(event(1))
            again = await records.add_event(event(1))  # new id, same idempotency key
            stored = await records.recent_events()
            return first, again, len(stored)

    assert run(body()) == (True, False, 1)


def test_other_tenant_sees_nothing(test_db: ScratchDb) -> None:
    async def body() -> tuple[Event | None, list[Event]]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            stored = event()
            await records.add_event(stored)
            elsewhere = Records(db.scoped(uuid4(), PROJECT))
            return await elsewhere.get_event(stored.id), await elsewhere.recent_events()

    assert run(body()) == (None, [])


def test_recent_events_are_newest_first_and_bounded(test_db: ScratchDb) -> None:
    async def body() -> tuple[list[object], int, int]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            for n in range(5):
                await records.add_event(event(n))
            newest = [e.payload["n"] for e in await records.recent_events(limit=3)]
            at_least_one = len(await records.recent_events(limit=0))
            capped = len(await records.recent_events(limit=MAX_PAGE + 10))
            return newest, at_least_one, capped

    assert run(body()) == ([4, 3, 2], 1, 5)


def test_run_finding_and_action_round_trip(test_db: ScratchDb) -> None:
    async def body() -> tuple[bool, bool, bool]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            stored_run = run_record()
            await records.add_run(stored_run)
            stored_finding = finding(stored_run.id)
            await records.add_finding(stored_finding)
            stored_action = action(stored_run.id)
            assert await records.add_action(stored_action) is True
            return (
                await records.get_run(stored_run.id) == stored_run,
                await records.get_finding(stored_finding.id) == stored_finding,
                await records.get_action(stored_action.id) == stored_action,
            )

    assert run(body()) == (True, True, True)


def test_run_without_model_and_collector_finding_round_trip(test_db: ScratchDb) -> None:
    async def body() -> tuple[bool, bool]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            bare = run_record(
                model=None, trigger_event_ids=(), outcome=RunOutcome.COULD_NOT_RUN, error_code="x.down"
            )
            await records.add_run(bare)
            collected = finding(None)
            await records.add_finding(collected)
            return (
                await records.get_run(bare.id) == bare,
                await records.get_finding(collected.id) == collected,
            )

    assert run(body()) == (True, True)


def test_approved_action_round_trip_keeps_the_approver(test_db: ScratchDb) -> None:
    async def body() -> bool:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            parent = run_record()
            await records.add_run(parent)
            approved = action(parent.id, status=ActionStatus.APPROVED, approved_by=uuid4(), approved_at=NOW)
            await records.add_action(approved)
            return await records.get_action(approved.id) == approved

    assert run(body()) is True


def test_same_action_is_proposed_once(test_db: ScratchDb) -> None:
    async def body() -> tuple[bool, bool]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            parent = run_record()
            await records.add_run(parent)
            return await records.add_action(action(parent.id)), await records.add_action(action(parent.id))

    assert run(body()) == (True, False)


def test_missing_records_are_none(test_db: ScratchDb) -> None:
    async def body() -> list[object]:
        async with open_db(test_db.app_url) as db:
            records = Records(db.scoped(TENANT, PROJECT))
            missing = uuid4()
            return [
                await records.get_run(missing),
                await records.get_finding(missing),
                await records.get_action(missing),
            ]

    assert run(body()) == [None, None, None]


def test_naive_run_start_time_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            naive = datetime(2026, 10, 17, 9, 0)  # noqa: DTZ001  # the point of the test
            await Records(db.scoped(TENANT, PROJECT)).add_run(run_record(started_at=naive))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.NAIVE_TIME


def test_action_for_an_unknown_run_is_rejected(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await Records(db.scoped(TENANT, PROJECT)).add_action(action(uuid4()))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_duplicate_run_is_a_conflict_and_the_error_carries_no_values(test_db: ScratchDb) -> None:
    stored = run_record()

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await Records(db.scoped(TENANT, PROJECT)).add_run(stored)
            await Records(db.scoped(TENANT, PROJECT)).add_run(stored)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.CONFLICT
    assert str(stored.id) not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_database_rules_back_up_the_sdk_rules(test_db: ScratchDb) -> None:
    """Even a row written around the SDK models cannot be approved without an approver."""

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            parent = run_record()
            await Records(db.scoped(TENANT, PROJECT)).add_run(parent)
            async with db.scoped(TENANT, PROJECT).connection() as conn:
                await conn.execute(
                    "INSERT INTO agentzaaheen.actions (id, tenant_id, project_id, run_id, tool,"
                    " level, payload,"
                    " target_kind, target_ref, data_classes, leak_check_passed, idempotency_key, status)"
                    " VALUES ($1, $2, $3, $4, 'email.send', 'act', '{}', 'ticket', 't:1', '{}', false, 'k',"
                    " 'approved')",
                    uuid4(),
                    TENANT,
                    PROJECT,
                    parent.id,
                )

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED
