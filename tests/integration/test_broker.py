"""The broker: a key saved sealed opens again for its own slot only, with its expiry beside it."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from yaqzan.core.broker import BrokerError, BrokerErrorCode, open_credential, save_credential
from yaqzan.core.store import AllSealedSecrets, CredentialDates, SlotKey, StoreError, StoreErrorCode
from yaqzan.core.vault import CredentialSlot, Keyring, MasterKey, VaultError, VaultErrorCode

TENANT = uuid4()
PROJECT = uuid4()


@pytest.fixture(autouse=True)
def _workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT))


SECRET = "-".join(["fake", "paddle", "key", "for", "broker", "tests"])  # built at runtime, never real
ENDS = datetime(2027, 1, 16, 9, 0, tzinfo=UTC)
RING = Keyring([MasterKey.generate("laptop-test")], "laptop-test")


def slot(environment: str = "production", project: UUID = PROJECT) -> CredentialSlot:
    return CredentialSlot.model_validate(
        {
            "tenant_id": TENANT,
            "project_id": project,
            "connector": "paddle",
            "name": "api_key",
            "environment": environment,
        }
    )


def test_a_saved_key_opens_and_its_dates_list_without_ciphertext(test_db: ScratchDb) -> None:
    async def body() -> tuple[SecretStr, list[CredentialDates]]:
        async with open_db(test_db.app_url) as db:
            here = db.scoped(TENANT, PROJECT)
            await save_credential(here, RING, slot(), SecretStr(SECRET), expires_at=ENDS, access="read_only")
            return await open_credential(here, RING, slot()), await AllSealedSecrets(
                db.scoped(TENANT)
            ).dates()

    opened, dates = run(body())
    assert opened.get_secret_value() == SECRET
    assert len(dates) == 1
    assert dates[0].slot == SlotKey(**slot().model_dump(mode="python"))
    assert (dates[0].expires_at, dates[0].access) == (ENDS, "read_only")
    assert SECRET not in repr(dates)


def test_saving_again_replaces_the_key_and_its_facts(test_db: ScratchDb) -> None:
    other = uuid4()

    async def body() -> tuple[SecretStr, list[CredentialDates]]:
        await add_workspaces(test_db.app_url, TENANT, other)
        async with open_db(test_db.app_url) as db:
            first, second = SecretStr("old-" + SECRET), SecretStr(SECRET)
            there = db.scoped(TENANT, other)
            await save_credential(there, RING, slot(project=other), first, expires_at=ENDS, access="unknown")
            await save_credential(
                there, RING, slot(project=other), second, expires_at=None, access="read_only"
            )
            dates = [
                d for d in await AllSealedSecrets(db.scoped(TENANT)).dates() if d.slot.project_id == other
            ]
            return await open_credential(there, RING, slot(project=other)), dates

    opened, dates = run(body())
    assert opened.get_secret_value() == SECRET
    assert [(d.expires_at, d.access) for d in dates] == [(None, "read_only")]


def test_a_missing_key_fails_closed_with_a_code(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            nowhere = uuid4()
            await open_credential(db.scoped(TENANT, nowhere), RING, slot(project=nowhere))

    with pytest.raises(BrokerError) as caught:
        run(body())
    assert caught.value.code is BrokerErrorCode.MISSING


def test_a_key_never_opens_under_another_master_key(test_db: ScratchDb) -> None:
    other = Keyring([MasterKey.generate("laptop-test")], "laptop-test")  # same id, different bytes
    where = slot("staging")

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            here = db.scoped(TENANT, PROJECT)
            await save_credential(here, RING, where, SecretStr(SECRET), expires_at=None, access="unknown")
            await open_credential(here, other, where)

    with pytest.raises(VaultError) as caught:
        run(body())
    assert caught.value.code is VaultErrorCode.TAMPERED
    assert SECRET not in str(caught.value)


def test_a_naive_expiry_is_refused_before_anything_is_stored(test_db: ScratchDb) -> None:
    naive = datetime(2027, 1, 16, 9, 0)  # noqa: DTZ001  # deliberately naive
    where = slot(project=uuid4())

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises(ValueError, match="timezone"):
                await save_credential(
                    db.scoped(TENANT, where.project_id),
                    RING,
                    where,
                    SecretStr(SECRET),
                    expires_at=naive,
                    access="unknown",
                )
            with pytest.raises(BrokerError):
                await open_credential(db.scoped(TENANT, where.project_id), RING, where)

    run(body())


def test_a_key_of_another_workspace_is_refused_not_opened(test_db: ScratchDb) -> None:
    """The handle decides the workspace: a slot naming another one is refused, not looked up."""
    neighbour = uuid4()

    async def body() -> None:
        await add_workspaces(test_db.app_url, TENANT, neighbour)
        async with open_db(test_db.app_url) as db:
            await save_credential(
                db.scoped(TENANT, neighbour),
                RING,
                slot(project=neighbour),
                SecretStr(SECRET),
                expires_at=None,
                access="unknown",
            )
            await open_credential(db.scoped(TENANT, PROJECT), RING, slot(project=neighbour))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.DENIED
