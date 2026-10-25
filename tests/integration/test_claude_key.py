"""The Claude key, sealed after Windows Hello and opened per call, on a real database (F10 slice 2c).

A signature is asked for one save of one key (``step:claude-key:<workspace>:<hash>``).
It fits only that key, only once. The database keeps only ciphertext; the key is
never in the audit log. Opening fails closed as a ``SourceError``, never a crash.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.broker import SealedSlot
from agentzaaheen.core.policy import ClaudeKeyControl, ClaudeKeyVerdict, PasskeyApprovals, RegistryControl
from agentzaaheen.core.policy.claude_key import CONNECTOR, NAME
from agentzaaheen.core.sdk import SourceError
from agentzaaheen.core.store import Database
from agentzaaheen.core.vault import CredentialSlot, Keyring, MasterKey
from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_passkey_approvals import CODE, SITE, Clock, enrol, registry

TENANT = UUID("00000000-0000-4000-8000-0000000000e1")
HOME = UUID("00000000-0000-4000-8000-0000000000e2")
KEY = SecretStr("sk-ant-api03-" + "k" * 40)  # built here, never a real key
OTHER = SecretStr("sk-ant-api03-" + "z" * 40)
RING = Keyring([MasterKey.generate("test-1")], current_id="test-1")


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, HOME))


def slot(db: Database, keyring: Keyring | None = RING) -> SealedSlot:
    where = CredentialSlot(
        tenant_id=TENANT, project_id=HOME, connector=CONNECTOR, name=NAME, environment="production"
    )
    return SealedSlot(db.scoped(TENANT, HOME), keyring, where, access="read_write")


def keys[T](
    test_db: ScratchDb,
    use: Callable[[ClaudeKeyControl, SoftAuthenticator, Clock], Awaitable[T]],
    *,
    keyring: Keyring | None = RING,
) -> T:
    """The Claude key control over the real passkey, with the passkey already set up."""

    async def body() -> T:
        clock, device = Clock(), SoftAuthenticator()
        async with open_db(test_db.app_url) as db:
            passkeys = PasskeyApprovals(
                db, TENANT, RegistryControl(db, TENANT, registry()), SITE, enrol_code=CODE, clock=clock
            )
            await enrol(passkeys, device)
            control = ClaudeKeyControl(
                db, TENANT, HOME, slot=slot(db, keyring), passkeys=passkeys, clock=clock
            )
            return await use(control, device, clock)

    return run(body())


async def signed_save(
    control: ClaudeKeyControl, device: SoftAuthenticator, clock: Clock, key: SecretStr
) -> ClaudeKeyVerdict:
    clock.now += timedelta(seconds=1)
    return await control.save(key, device.sign(await control.options(key)))


def stored(test_db: ScratchDb) -> list[bytes]:
    async def body() -> list[bytes]:
        conn = await connect_as(test_db.app_url, TENANT, HOME)
        try:
            rows = await conn.fetch("SELECT wrapped_dek, ciphertext FROM agentzaaheen.sealed_secrets")
        finally:
            await conn.close()
        return [bytes(r["wrapped_dek"]) + bytes(r["ciphertext"]) for r in rows]

    return run(body())


def audit(test_db: ScratchDb) -> list[tuple[str, str, str, str]]:
    async def body() -> list[tuple[str, str, str, str]]:
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            rows = await conn.fetch(
                "SELECT action, outcome, reason_code, coalesce(target_ref, '') || details::text AS rest"
                " FROM agentzaaheen.audit_log WHERE tenant_id = $1 ORDER BY seq",
                TENANT,
            )
        finally:
            await conn.close()
        return [(r["action"], r["outcome"], r["reason_code"], r["rest"]) for r in rows]

    return run(body())


def test_a_signed_save_seals_the_key_and_each_call_opens_it(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, device: SoftAuthenticator, clock: Clock) -> list[object]:
        before = await control.status()
        unsigned = await control.save(KEY, None)
        done = await signed_save(control, device, clock, KEY)
        after = await control.status()
        opened = await control.key_source()()
        return [
            before.saved,
            unsigned,
            done,
            after.vault,
            after.saved,
            after.saved_at is not None,
            opened.get_secret_value(),
        ]

    assert keys(test_db, use) == [
        False,
        ClaudeKeyVerdict.NEEDS_PASSKEY,
        ClaudeKeyVerdict.SAVED,
        True,
        True,
        True,
        KEY.get_secret_value(),
    ]
    (row,) = stored(test_db)
    assert KEY.get_secret_value().encode() not in row  # ciphertext only
    lines = audit(test_db)
    assert ("claude_key.save", "allow", "claude_key.saved", "credential:claude:api_key{}") in lines
    assert not any(KEY.get_secret_value() in str(line) for line in lines)


def test_a_signature_for_one_key_cannot_save_another(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, device: SoftAuthenticator, clock: Clock) -> list[object]:
        clock.now += timedelta(seconds=1)
        signed = device.sign(await control.options(KEY))
        swapped = await control.save(OTHER, signed)
        saved_after_swap = (await control.status()).saved
        meant = await control.save(KEY, signed)  # the key it was signed for still saves, once
        again = await control.save(KEY, signed)
        return [swapped, saved_after_swap, meant, again, (await control.key_source()()).get_secret_value()]

    assert keys(test_db, use) == [
        ClaudeKeyVerdict.PASSKEY_FAILED,
        False,
        ClaudeKeyVerdict.SAVED,
        ClaudeKeyVerdict.PASSKEY_FAILED,
        KEY.get_secret_value(),
    ]
    assert audit(test_db)[-1][:3] == ("claude_key.save", "deny", "claude_key.passkey_failed")


def test_a_second_signed_save_replaces_the_key(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, device: SoftAuthenticator, clock: Clock) -> str:
        await signed_save(control, device, clock, KEY)
        await signed_save(control, device, clock, OTHER)
        return (await control.key_source()()).get_secret_value()

    assert keys(test_db, use) == OTHER.get_secret_value()
    assert len(stored(test_db)) == 1


def test_something_that_is_not_a_claude_key_is_refused_before_windows_hello(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, _: SoftAuthenticator, __: Clock) -> list[object]:
        with pytest.raises(PasskeyError) as asked:
            await control.options(SecretStr("sk-proj-not-claude"))
        return [asked.value.code, await control.save(SecretStr("sk-ant- spaced"), {"id": "x"})]

    assert keys(test_db, use) == ["claude_key.bad_key", ClaudeKeyVerdict.BAD_KEY]
    assert audit(test_db)[-1][:3] == ("claude_key.save", "deny", "claude_key.bad_key")
    assert "sk-ant" not in str(audit(test_db))


def test_without_the_vault_nothing_is_saved_and_opening_fails_closed(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, _: SoftAuthenticator, __: Clock) -> list[object]:
        with pytest.raises(PasskeyError) as asked:
            await control.options(KEY)
        with pytest.raises(SourceError) as opened:
            await control.key_source()()
        return [
            (await control.status()).vault,
            asked.value.code,
            await control.save(KEY, {"id": "x"}),
            opened.value.reason_code,
        ]

    assert keys(test_db, use, keyring=None) == [
        False,
        "claude_key.no_vault",
        ClaudeKeyVerdict.NO_VAULT,
        "vault.bad_key_file",
    ]
    assert stored(test_db) == []


def test_with_no_key_saved_a_call_ends_as_not_reached_not_a_crash(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, _: SoftAuthenticator, __: Clock) -> str | None:
        with pytest.raises(SourceError) as opened:
            await control.key_source()()
        return opened.value.reason_code

    assert keys(test_db, use) == "credential.missing"


def test_a_key_sealed_under_another_master_key_does_not_open(test_db: ScratchDb) -> None:
    async def use(control: ClaudeKeyControl, device: SoftAuthenticator, clock: Clock) -> str | None:
        await signed_save(control, device, clock, KEY)
        return None

    keys(test_db, use)

    async def body() -> str | None:
        stranger = Keyring([MasterKey.generate("other-1")], current_id="other-1")
        async with open_db(test_db.app_url) as db:
            with pytest.raises(SourceError) as opened:
                await slot(db, stranger).open()
        return opened.value.reason_code

    assert run(body()) == "vault.unknown_key"
