"""The server access token, sealed after Windows Hello and opened per call, on a real database (F10 slice 4).

A signature is asked for one save of one token (``step:server-token:<workspace>:<hash>``),
covering both parts. It fits only that token, only once. The database keeps only
ciphertext; neither part is ever in the audit log. Opening fails closed as a
``SourceError``, never a crash.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from pydantic import SecretStr

from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.broker import SealedSlot
from agentzaaheen.core.policy import (
    PasskeyApprovals,
    RegistryControl,
    ServerTokenControl,
    ServerTokenVerdict,
    ServiceToken,
)
from agentzaaheen.core.policy.server_token import CONNECTOR, NAME
from agentzaaheen.core.sdk import SourceError
from agentzaaheen.core.store import Database
from agentzaaheen.core.vault import CredentialSlot, Keyring
from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_claude_key import HOME, RING, TENANT, audit, stored
from tests.integration.test_passkey_approvals import CODE, SITE, Clock, enrol, registry

ID = "0" * 32 + ".access"  # built here, never a real token
SECRET = SecretStr("f" * 64)
OTHER_ID = "1" * 32 + ".access"
OTHER = SecretStr("e" * 64)


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, HOME))


def slot(db: Database, keyring: Keyring | None = RING) -> SealedSlot:
    where = CredentialSlot(
        tenant_id=TENANT, project_id=HOME, connector=CONNECTOR, name=NAME, environment="production"
    )
    return SealedSlot(db.scoped(TENANT, HOME), keyring, where, access="read_write")


def tokens[T](
    test_db: ScratchDb,
    use: Callable[[ServerTokenControl, SoftAuthenticator, Clock], Awaitable[T]],
    *,
    keyring: Keyring | None = RING,
) -> T:
    """The server token control over the real passkey, with the passkey already set up."""

    async def body() -> T:
        clock, device = Clock(), SoftAuthenticator()
        async with open_db(test_db.app_url) as db:
            passkeys = PasskeyApprovals(
                db, TENANT, RegistryControl(db, TENANT, registry()), SITE, enrol_code=CODE, clock=clock
            )
            await enrol(passkeys, device)
            control = ServerTokenControl(
                db, TENANT, HOME, slot=slot(db, keyring), passkeys=passkeys, clock=clock
            )
            return await use(control, device, clock)

    return run(body())


async def signed_save(
    control: ServerTokenControl, device: SoftAuthenticator, clock: Clock, client_id: str, secret: SecretStr
) -> ServerTokenVerdict:
    clock.now += timedelta(seconds=1)
    return await control.save(client_id, secret, device.sign(await control.options(client_id, secret)))


def parts(token: ServiceToken) -> tuple[str, str]:
    return token.client_id, token.client_secret.get_secret_value()


def test_a_signed_save_seals_both_parts_and_each_call_opens_them(test_db: ScratchDb) -> None:
    async def use(control: ServerTokenControl, device: SoftAuthenticator, clock: Clock) -> list[object]:
        before = await control.status()
        unsigned = await control.save(ID, SECRET, None)
        done = await signed_save(control, device, clock, ID, SECRET)
        after = await control.status()
        return [
            before.saved,
            unsigned,
            done,
            after.vault,
            after.saved,
            after.saved_at is not None,
            parts(await control.token_source()()),
        ]

    assert tokens(test_db, use) == [
        False,
        ServerTokenVerdict.NEEDS_PASSKEY,
        ServerTokenVerdict.SAVED,
        True,
        True,
        True,
        (ID, SECRET.get_secret_value()),
    ]
    (row,) = stored(test_db)
    assert SECRET.get_secret_value().encode() not in row  # ciphertext only
    assert ID.encode() not in row
    lines = audit(test_db)
    assert (
        "server_token.save",
        "allow",
        "server_token.saved",
        "credential:cloudflare_access:service_token{}",
    ) in lines
    assert not any(SECRET.get_secret_value() in str(line) or ID in str(line) for line in lines)


def test_a_signature_for_one_token_cannot_save_another(test_db: ScratchDb) -> None:
    async def use(control: ServerTokenControl, device: SoftAuthenticator, clock: Clock) -> list[object]:
        clock.now += timedelta(seconds=1)
        signed = device.sign(await control.options(ID, SECRET))
        other_secret = await control.save(ID, OTHER, signed)
        other_id = await control.save(OTHER_ID, SECRET, signed)
        saved_after_swaps = (await control.status()).saved
        meant = await control.save(ID, SECRET, signed)  # the token it was signed for still saves, once
        again = await control.save(ID, SECRET, signed)
        return [other_secret, other_id, saved_after_swaps, meant, again]

    assert tokens(test_db, use) == [
        ServerTokenVerdict.PASSKEY_FAILED,
        ServerTokenVerdict.PASSKEY_FAILED,
        False,
        ServerTokenVerdict.SAVED,
        ServerTokenVerdict.PASSKEY_FAILED,
    ]
    assert audit(test_db)[-1][:3] == ("server_token.save", "deny", "server_token.passkey_failed")


def test_a_second_signed_save_replaces_both_parts(test_db: ScratchDb) -> None:
    async def use(control: ServerTokenControl, device: SoftAuthenticator, clock: Clock) -> tuple[str, str]:
        await signed_save(control, device, clock, ID, SECRET)
        await signed_save(control, device, clock, OTHER_ID, OTHER)
        return parts(await control.token_source()())

    assert tokens(test_db, use) == (OTHER_ID, OTHER.get_secret_value())
    assert len(stored(test_db)) == 1


@pytest.mark.parametrize(
    ("client_id", "secret"),
    [
        ("abc123", "f" * 64),  # not a Client ID
        (ID, ID),  # swapped, or the ID pasted twice
        (ID, "f" * 32 + " " + "f" * 31),  # a space
        (ID, "f" * 32 + "\n" + "f" * 31),  # a line break would split the sealed pair wrongly
        (ID, ""),
    ],
)
def test_something_that_is_not_a_token_is_refused_before_windows_hello(
    test_db: ScratchDb, client_id: str, secret: str
) -> None:
    async def use(control: ServerTokenControl, _: SoftAuthenticator, __: Clock) -> list[object]:
        with pytest.raises(PasskeyError) as asked:
            await control.options(client_id, SecretStr(secret))
        return [asked.value.code, await control.save(client_id, SecretStr(secret), {"id": "x"})]

    assert tokens(test_db, use) == ["server_token.bad_token", ServerTokenVerdict.BAD_TOKEN]
    assert audit(test_db)[-1][:3] == ("server_token.save", "deny", "server_token.bad_token")
    assert stored(test_db) == []


def test_without_the_vault_nothing_is_saved_and_opening_fails_closed(test_db: ScratchDb) -> None:
    async def use(control: ServerTokenControl, _: SoftAuthenticator, __: Clock) -> list[object]:
        with pytest.raises(PasskeyError) as asked:
            await control.options(ID, SECRET)
        with pytest.raises(SourceError) as opened:
            await control.token_source()()
        return [
            (await control.status()).vault,
            asked.value.code,
            await control.save(ID, SECRET, {"id": "x"}),
            opened.value.reason_code,
        ]

    assert tokens(test_db, use, keyring=None) == [
        False,
        "server_token.no_vault",
        ServerTokenVerdict.NO_VAULT,
        "vault.bad_key_file",
    ]
    assert stored(test_db) == []


def test_with_no_token_saved_opening_fails_closed(test_db: ScratchDb) -> None:
    async def use(control: ServerTokenControl, _: SoftAuthenticator, __: Clock) -> str | None:
        with pytest.raises(SourceError) as opened:
            await control.token_source()()
        return opened.value.reason_code

    assert tokens(test_db, use) == "credential.missing"


def test_something_sealed_in_the_slot_that_is_not_a_token_does_not_open(test_db: ScratchDb) -> None:
    async def use(control: ServerTokenControl, _: SoftAuthenticator, __: Clock) -> str:
        async with open_db(test_db.app_url) as db:
            await slot(db).save(SecretStr("only-one-part"))
        with pytest.raises(SourceError) as opened:
            await control.token_source()()
        return opened.value.code.value

    assert tokens(test_db, use) == "source.bad_response"
