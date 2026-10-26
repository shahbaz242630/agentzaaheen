"""Our server model's key, sealed after Windows Hello and opened per call, on a real database (F10 slice 6).

A signature is asked for one save of one key (``step:model-key:<workspace>:<hash>``).
It fits only that key, only once. The database keeps only ciphertext; the key
is never in the audit log. Opening fails closed as a ``SourceError``, never a
crash. The key has its own slot: saving it never touches the server token or
the Claude key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from pydantic import SecretStr

from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.broker import SealedSlot
from agentzaaheen.core.policy import ModelKeyControl, ModelKeyVerdict, PasskeyApprovals, RegistryControl
from agentzaaheen.core.policy.model_key import CONNECTOR, NAME
from agentzaaheen.core.sdk import SourceError
from agentzaaheen.core.store import Database
from agentzaaheen.core.vault import CredentialSlot, Keyring
from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_claude_key import HOME, RING, TENANT, audit, stored
from tests.integration.test_passkey_approvals import CODE, SITE, Clock, enrol, registry

KEY = SecretStr("a1" * 32)  # built here, never the real one
OTHER = SecretStr("b2" * 32)


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
    use: Callable[[ModelKeyControl, SoftAuthenticator, Clock], Awaitable[T]],
    *,
    keyring: Keyring | None = RING,
) -> T:
    """The model key control over the real passkey, with the passkey already set up."""

    async def body() -> T:
        clock, device = Clock(), SoftAuthenticator()
        async with open_db(test_db.app_url) as db:
            passkeys = PasskeyApprovals(
                db, TENANT, RegistryControl(db, TENANT, registry()), SITE, enrol_code=CODE, clock=clock
            )
            await enrol(passkeys, device)
            control = ModelKeyControl(
                db, TENANT, HOME, slot=slot(db, keyring), passkeys=passkeys, clock=clock
            )
            return await use(control, device, clock)

    return run(body())


async def signed_save(
    control: ModelKeyControl, device: SoftAuthenticator, clock: Clock, key: SecretStr
) -> ModelKeyVerdict:
    clock.now += timedelta(seconds=1)
    return await control.save(key, device.sign(await control.options(key)))


def test_a_signed_save_seals_the_key_and_each_call_opens_it(test_db: ScratchDb) -> None:
    async def use(control: ModelKeyControl, device: SoftAuthenticator, clock: Clock) -> list[object]:
        before = await control.status()
        unsigned = await control.save(KEY, None)
        done = await signed_save(control, device, clock, KEY)
        after = await control.status()
        return [
            before.saved,
            unsigned,
            done,
            after.vault,
            after.saved,
            after.saved_at is not None,
            (await control.key_source()()).get_secret_value(),
        ]

    assert keys(test_db, use) == [
        False,
        ModelKeyVerdict.NEEDS_PASSKEY,
        ModelKeyVerdict.SAVED,
        True,
        True,
        True,
        KEY.get_secret_value(),
    ]
    (row,) = stored(test_db)
    assert KEY.get_secret_value().encode() not in row  # ciphertext only
    lines = audit(test_db)
    assert ("model_key.save", "allow", "model_key.saved", "credential:server_model:api_key{}") in lines
    assert not any(KEY.get_secret_value() in str(line) for line in lines)


def test_a_signature_for_one_key_cannot_save_another(test_db: ScratchDb) -> None:
    async def use(control: ModelKeyControl, device: SoftAuthenticator, clock: Clock) -> list[object]:
        clock.now += timedelta(seconds=1)
        signed = device.sign(await control.options(KEY))
        other = await control.save(OTHER, signed)
        saved_after_swap = (await control.status()).saved
        meant = await control.save(KEY, signed)  # the key it was signed for still saves, once
        again = await control.save(KEY, signed)
        return [other, saved_after_swap, meant, again]

    assert keys(test_db, use) == [
        ModelKeyVerdict.PASSKEY_FAILED,
        False,
        ModelKeyVerdict.SAVED,
        ModelKeyVerdict.PASSKEY_FAILED,
    ]
    assert audit(test_db)[-1][:3] == ("model_key.save", "deny", "model_key.passkey_failed")


def test_a_second_signed_save_replaces_the_key(test_db: ScratchDb) -> None:
    async def use(control: ModelKeyControl, device: SoftAuthenticator, clock: Clock) -> str:
        await signed_save(control, device, clock, KEY)
        await signed_save(control, device, clock, OTHER)
        return (await control.key_source()()).get_secret_value()

    assert keys(test_db, use) == OTHER.get_secret_value()
    assert len(stored(test_db)) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "short",
        "a1" * 16 + " " + "a1" * 16,  # a space
        "sk-ant-" + "a" * 40,  # the Claude key pasted in the wrong box
        "0" * 32 + ".access",  # the Cloudflare Client ID pasted in the wrong box
        "",
    ],
)
def test_something_that_is_not_the_key_is_refused_before_windows_hello(test_db: ScratchDb, raw: str) -> None:
    async def use(control: ModelKeyControl, _: SoftAuthenticator, __: Clock) -> list[object]:
        with pytest.raises(PasskeyError) as asked:
            await control.options(SecretStr(raw))
        return [asked.value.code, await control.save(SecretStr(raw), {"id": "x"})]

    assert keys(test_db, use) == ["model_key.bad_key", ModelKeyVerdict.BAD_KEY]
    assert audit(test_db)[-1][:3] == ("model_key.save", "deny", "model_key.bad_key")
    assert stored(test_db) == []


def test_without_the_vault_nothing_is_saved_and_opening_fails_closed(test_db: ScratchDb) -> None:
    async def use(control: ModelKeyControl, _: SoftAuthenticator, __: Clock) -> list[object]:
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
        "model_key.no_vault",
        ModelKeyVerdict.NO_VAULT,
        "vault.bad_key_file",
    ]
    assert stored(test_db) == []


def test_with_no_key_saved_opening_fails_closed(test_db: ScratchDb) -> None:
    async def use(control: ModelKeyControl, _: SoftAuthenticator, __: Clock) -> str | None:
        with pytest.raises(SourceError) as opened:
            await control.key_source()()
        return opened.value.reason_code

    assert keys(test_db, use) == "credential.missing"


def test_something_sealed_in_the_slot_that_is_not_a_key_does_not_open(test_db: ScratchDb) -> None:
    async def use(control: ModelKeyControl, _: SoftAuthenticator, __: Clock) -> str:
        async with open_db(test_db.app_url) as db:
            await slot(db).save(SecretStr("not a key"))
        with pytest.raises(SourceError) as opened:
            await control.key_source()()
        return opened.value.code.value

    assert keys(test_db, use) == "source.bad_response"
