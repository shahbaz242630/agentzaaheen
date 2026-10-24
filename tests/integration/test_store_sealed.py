"""Sealed credentials: the database keeps only vault ciphertext, and it still opens after a reload."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from yaqzan.core.store import AllSealedSecrets, SealedRow, SealedSecrets, SlotKey
from yaqzan.core.vault import CredentialSlot, Keyring, MasterKey, SealedSecret, open_secret, rewrap, seal

TENANT = uuid4()
PROJECT = uuid4()


@pytest.fixture(autouse=True)
def _workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT))


PLAINTEXT = "-".join(["plain", "credential", "value", "for", "tests"])  # fake, built at runtime


def slot(name: str = "api_key") -> CredentialSlot:
    return CredentialSlot(
        tenant_id=TENANT, project_id=PROJECT, connector="paddle", name=name, environment="staging"
    )


def key_of(s: CredentialSlot) -> SlotKey:
    return SlotKey(**s.model_dump(mode="python"))


def row_of(s: CredentialSlot, sealed: SealedSecret) -> SealedRow:
    return SealedRow(
        slot=key_of(s),
        format_version=sealed.format_version,
        master_key_id=sealed.master_key_id,
        wrapped_dek=sealed.wrapped_dek,
        ciphertext=sealed.ciphertext,
    )


def sealed_of(row: SealedRow) -> SealedSecret:
    return SealedSecret(
        format_version=1,
        master_key_id=row.master_key_id,
        wrapped_dek=row.wrapped_dek,
        ciphertext=row.ciphertext,
    )


def keyring(*ids: str) -> Keyring:
    return Keyring([MasterKey.generate(i) for i in ids], current_id=ids[-1])


def test_sealed_secret_opens_after_save_and_reload(test_db: ScratchDb) -> None:
    ring = keyring("k1")
    stored = row_of(slot(), seal(SecretStr(PLAINTEXT), slot(), ring))

    async def body() -> SealedRow | None:
        async with open_db(test_db.app_url) as db:
            await SealedSecrets(db.scoped(TENANT, PROJECT)).put(stored)
        async with open_db(test_db.app_url) as db:
            return await SealedSecrets(db.scoped(TENANT, PROJECT)).get(key_of(slot()))

    loaded = run(body())
    assert loaded == stored
    assert open_secret(sealed_of(loaded), slot(), ring).get_secret_value() == PLAINTEXT


def test_plaintext_never_reaches_the_table(test_db: ScratchDb) -> None:
    ring = keyring("k1")

    async def body() -> bytes:
        async with open_db(test_db.app_url) as db:
            await SealedSecrets(db.scoped(TENANT, PROJECT)).put(
                row_of(slot(), seal(SecretStr(PLAINTEXT), slot(), ring))
            )
            async with db.scoped(TENANT, PROJECT).connection() as conn:
                dump = await conn.fetchval(
                    "SELECT string_agg(t::text, '|')::bytea FROM yaqzan.sealed_secrets t"
                )
                return bytes(dump)

    dump = run(body())
    assert PLAINTEXT.encode() not in dump


def test_put_replaces_and_delete_removes(test_db: ScratchDb) -> None:
    ring = keyring("k1")

    async def body() -> tuple[str, bool, bool, SealedRow | None]:
        async with open_db(test_db.app_url) as db:
            repo = SealedSecrets(db.scoped(TENANT, PROJECT))
            await repo.put(row_of(slot(), seal(SecretStr("first-value"), slot(), ring)))
            await repo.put(row_of(slot(), seal(SecretStr("second-value"), slot(), ring)))
            current = await repo.get(key_of(slot()))
            assert current is not None
            value = open_secret(sealed_of(current), slot(), ring).get_secret_value()
            return (
                value,
                await repo.delete(key_of(slot())),
                await repo.delete(key_of(slot())),
                await repo.get(key_of(slot())),
            )

    assert run(body()) == ("second-value", True, False, None)


def test_rows_left_under_an_old_master_key_are_found_for_rewrap(test_db: ScratchDb) -> None:
    first = MasterKey.generate("k1")
    old = Keyring([first], current_id="k1")
    rotated = Keyring([first, MasterKey.generate("k2")], current_id="k2")

    async def body() -> tuple[list[str], list[str]]:
        async with open_db(test_db.app_url) as db:
            repo = SealedSecrets(db.scoped(TENANT, PROJECT))
            for name in ("api_key", "webhook_key"):
                await repo.put(row_of(slot(name), seal(SecretStr(PLAINTEXT), slot(name), old)))
            everywhere = AllSealedSecrets(db.scoped(TENANT))
            stale = await everywhere.not_under_key("k2")
            for row in stale:
                s = slot(row.slot.name)
                await repo.put(row_of(s, rewrap(sealed_of(row), s, rotated)))
            return [r.slot.name for r in stale], [r.slot.name for r in await everywhere.not_under_key("k2")]

    assert run(body()) == (["api_key", "webhook_key"], [])


def test_row_repr_shows_no_ciphertext() -> None:
    row = SealedRow(
        slot=key_of(slot()),
        format_version=1,
        master_key_id="k1",
        wrapped_dek=b"\x01" * 60,
        ciphertext=b"\x02" * 40,
    )
    assert "\\x02" not in repr(row)
    assert "40 bytes" in str(row)
