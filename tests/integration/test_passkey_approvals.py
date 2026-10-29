"""Approving with a passkey, end to end on a real database (ADR-SEC-007), with a software authenticator.

Set-up needs the start-up code and works once; an approval needs a signature
for that very request; a stale request, a wrong signature, a replay or a
second use of a challenge are all refused and audited; only a good one
switches the item on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from agentzaaheen.core.auth import PasskeyError, Site
from agentzaaheen.core.policy import Channel, PasskeyApprovals, RegistryControl, Role, Verdict
from agentzaaheen.core.registry import Invocation, Kind, Registry, Skill, State
from agentzaaheen.core.store import PasskeyRow, PasskeyRows
from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import ScratchDb, connect_as, open_db, run

TENANT = uuid4()
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CODE = "0a1b2c3d"
SITE = Site(origin="http://localhost:8765")
FILES = "a" * 64
ME = {"role": Role.CHAIRMAN, "channel": Channel.DASHBOARD}
SKILL = "triage-failing-build"


def registry(files: str = FILES) -> Registry:
    skill = Skill(id=SKILL, description="x", invocation=Invocation.AUTO, requires=(), fingerprint=files)
    return Registry(skills=(skill,))


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def session[T](
    test_db: ScratchDb,
    use: Callable[[PasskeyApprovals, RegistryControl], Awaitable[T]],
    *,
    code: str | None = CODE,
    files: str = FILES,
    clock: Clock | None = None,
) -> T:
    async def body() -> T:
        async with open_db(test_db.app_url) as db:
            control = RegistryControl(db, TENANT, registry(files))
            passkeys = PasskeyApprovals(db, TENANT, control, SITE, enrol_code=code, clock=clock or Clock())
            return await use(passkeys, control)

    return run(body())


async def enrol(passkeys: PasskeyApprovals, device: SoftAuthenticator) -> None:
    await passkeys.enrol(CODE, device.register(await passkeys.enrol_options(CODE)))


async def asked(control: RegistryControl) -> UUID:
    await control.switch_off(Kind.SKILL, SKILL, now=NOW, **ME)  # type: ignore[arg-type]
    await control.request_on(Kind.SKILL, SKILL, now=NOW, **ME)  # type: ignore[arg-type]
    (pending,) = (await control.standing()).pending
    return pending.id


def audit(test_db: ScratchDb) -> list[tuple[str, str, str]]:
    async def body() -> list[tuple[str, str, str]]:
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            rows = await conn.fetch(
                "SELECT action, outcome, reason_code FROM agentzaaheen.audit_log WHERE tenant_id ="
                " $1 ORDER BY seq",
                TENANT,
            )
        finally:
            await conn.close()
        return [(r["action"], r["outcome"], r["reason_code"]) for r in rows]

    return run(body())


def test_set_up_needs_the_code_and_works_once(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> tuple[object, ...]:
        before = await passkeys.status()
        with pytest.raises(PasskeyError) as wrong:
            await passkeys.enrol_options("ffffffff")
        await enrol(passkeys, device)
        after = await passkeys.status()
        with pytest.raises(PasskeyError) as again:
            await passkeys.enrol_options(CODE)
        return before, wrong.value.code, after, again.value.code

    before, wrong, after, again = session(test_db, use)
    assert before == (False, True)
    assert wrong == "passkey.wrong_code"
    assert after == (True, False)
    assert again == "passkey.enrol_closed"
    assert ("passkey.enrol", "allow", "passkey.enrolled") in audit(test_db)


def test_without_the_start_up_code_there_is_no_set_up(test_db: ScratchDb) -> None:
    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> tuple[object, str]:
        with pytest.raises(PasskeyError) as closed:
            await passkeys.enrol_options(CODE)
        return await passkeys.status(), closed.value.code

    assert session(test_db, use, code=None) == ((False, False), "passkey.enrol_closed")


def test_five_wrong_codes_close_set_up(test_db: ScratchDb) -> None:
    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> str:
        for _ in range(5):
            with pytest.raises(PasskeyError):
                await passkeys.enrol_options("ffffffff")
        with pytest.raises(PasskeyError) as closed:
            await passkeys.enrol_options(CODE)  # even the right code now
        return closed.value.code

    assert session(test_db, use) == "passkey.enrol_closed"


def test_a_good_signature_approves_and_switches_on(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, control: RegistryControl) -> tuple[Verdict, State]:
        await enrol(passkeys, device)
        request_id = await asked(control)
        options = await passkeys.approve_options(request_id)
        verdict = await passkeys.approve(request_id, device.sign(options))
        return verdict, (await control.standing()).states[(Kind.SKILL, SKILL)]

    async def counter() -> int:
        async with open_db(test_db.app_url) as db:
            row = await PasskeyRows(db.scoped(TENANT)).active()
            assert row is not None
            return row.sign_count

    assert session(test_db, use) == (Verdict.DONE, State.ON)
    assert run(counter()) == 1
    assert ("registry.approve", "allow", "registry.done") in audit(test_db)


def test_a_signature_for_one_request_cannot_approve_another_or_be_replayed(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, control: RegistryControl) -> tuple[Verdict, Verdict, State]:
        await enrol(passkeys, device)
        request_id = await asked(control)
        signed = device.sign(await passkeys.approve_options(request_id))
        other = await passkeys.approve(uuid4(), signed)  # no challenge was issued for it
        await passkeys.approve(request_id, signed)
        replay = await passkeys.approve(request_id, signed)  # the challenge is used up
        return other, replay, (await control.standing()).states[(Kind.SKILL, SKILL)]

    other, replay, state = session(test_db, use)
    assert (other, replay) == (Verdict.PASSKEY_FAILED, Verdict.PASSKEY_FAILED)
    assert state is State.ON  # from the one good approval only


@pytest.mark.parametrize(
    "answer",
    [
        lambda d, o: d.sign(o, tamper=True),
        lambda d, o: d.sign(o, verified=False),
        lambda d, o: d.sign(o, origin="http://127.0.0.1:8765"),
        lambda d, o: SoftAuthenticator().sign(o),  # someone else's passkey
    ],
)
def test_a_bad_signature_switches_nothing_on_and_is_audited(test_db: ScratchDb, answer: object) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, control: RegistryControl) -> tuple[Verdict, State, int]:
        await enrol(passkeys, device)
        request_id = await asked(control)
        options = await passkeys.approve_options(request_id)
        verdict = await passkeys.approve(request_id, answer(device, options))  # type: ignore[operator]
        standing = await control.standing()
        return verdict, standing.states[(Kind.SKILL, SKILL)], len(standing.pending)

    assert session(test_db, use) == (Verdict.PASSKEY_FAILED, State.OFF, 1)
    assert audit(test_db)[-1][:2] == ("registry.approve", "deny")


def test_an_expired_challenge_is_refused(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()
    clock = Clock()

    async def use(passkeys: PasskeyApprovals, control: RegistryControl) -> Verdict:
        await enrol(passkeys, device)
        request_id = await asked(control)
        options = await passkeys.approve_options(request_id)
        clock.now = NOW + timedelta(minutes=3)
        return await passkeys.approve(request_id, device.sign(options))

    assert session(test_db, use, clock=clock) is Verdict.PASSKEY_FAILED
    assert audit(test_db)[-1] == ("registry.approve", "deny", "passkey.challenge_expired")


def test_a_stale_request_is_refused_before_any_passkey_is_asked(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def first(passkeys: PasskeyApprovals, control: RegistryControl) -> UUID:
        await enrol(passkeys, device)
        return await asked(control)

    request_id = session(test_db, first)

    async def later(passkeys: PasskeyApprovals, _c: RegistryControl) -> str:
        with pytest.raises(PasskeyError) as caught:
            await passkeys.approve_options(request_id)
        return caught.value.code

    assert session(test_db, later, files="b" * 64) == "registry.stale"


def test_no_passkey_means_nothing_can_be_approved(test_db: ScratchDb) -> None:
    async def use(passkeys: PasskeyApprovals, control: RegistryControl) -> str:
        request_id = await asked(control)
        with pytest.raises(PasskeyError) as caught:
            await passkeys.approve_options(request_id)
        return caught.value.code

    assert session(test_db, use) == "registry.no_passkey"


def test_approve_itself_needs_a_fresh_step_up_and_a_person(test_db: ScratchDb) -> None:
    async def use(_p: PasskeyApprovals, control: RegistryControl) -> list[Verdict]:
        request_id = await asked(control)
        old = NOW - timedelta(minutes=16)
        return [
            await control.approve(request_id, step_up_at=None, now=NOW, **ME),  # type: ignore[arg-type]
            await control.approve(request_id, step_up_at=old, now=NOW, **ME),  # type: ignore[arg-type]
            await control.approve(
                request_id, role=Role.AI_OPERATOR, channel=Channel.MCP, step_up_at=NOW, now=NOW
            ),
            await control.approve(request_id, step_up_at=NOW, now=NOW, **ME),  # type: ignore[arg-type]
        ]

    assert session(test_db, use) == [Verdict.NOT_ALLOWED] * 3 + [Verdict.DONE]


def test_refuse_closes_a_request_without_a_passkey(test_db: ScratchDb) -> None:
    async def use(_p: PasskeyApprovals, control: RegistryControl) -> tuple[Verdict, Verdict, Verdict, int]:
        request_id = await asked(control)
        done = await control.refuse(request_id, now=NOW, **ME)  # type: ignore[arg-type]
        again = await control.refuse(request_id, now=NOW, **ME)  # type: ignore[arg-type]
        late = await control.approve(request_id, step_up_at=NOW, now=NOW, **ME)  # type: ignore[arg-type]
        return done, again, late, len((await control.standing()).pending)

    assert session(test_db, use) == (Verdict.DONE, Verdict.UNKNOWN_REQUEST, Verdict.UNKNOWN_REQUEST, 0)


def test_the_app_cannot_swap_a_stored_public_key(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            row = PasskeyRow(b"i" * 16, b"k" * 64, 0, "user:chairman", NOW)
            rows = PasskeyRows(db.scoped(TENANT))
            assert await rows.add_first(row)
            assert not await rows.add_first(row)  # only the first
            assert await rows.record_use(b"i" * 16, 3, NOW)  # the counter moves forward
            assert not await rows.record_use(b"i" * 16, 2, NOW)  # never backwards
            assert not await rows.record_use(b"x" * 16, 9, NOW)  # an unknown passkey
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE agentzaaheen.passkeys SET public_key = $1", b"z" * 64)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM agentzaaheen.passkeys")
        finally:
            await conn.close()

    run(body())


def test_a_lost_passkey_is_replaced_with_a_new_start_up_code(test_db: ScratchDb) -> None:
    lost, new = SoftAuthenticator(), SoftAuthenticator()

    async def first(passkeys: PasskeyApprovals, control: RegistryControl) -> UUID:
        await enrol(passkeys, lost)
        return await asked(control)

    request_id = session(test_db, first)

    async def recover(passkeys: PasskeyApprovals, control: RegistryControl) -> tuple[object, ...]:
        before = await passkeys.status()
        await enrol(passkeys, new)  # the next start printed a new code
        old_sig = lost.sign(await passkeys.approve_options(request_id))
        old = await passkeys.approve(request_id, old_sig)
        good = await passkeys.approve(request_id, new.sign(await passkeys.approve_options(request_id)))
        return before, old, good, (await control.standing()).states[(Kind.SKILL, SKILL)]

    before, old, good, state = session(test_db, recover)
    assert before == (True, True)  # a passkey exists, and replacing it is open on this run
    assert (old, good, state) == (Verdict.PASSKEY_FAILED, Verdict.DONE, State.ON)
    assert ("passkey.enrol", "allow", "passkey.replaced") in audit(test_db)


def test_replacing_needs_the_code_like_the_first_set_up(test_db: ScratchDb) -> None:
    async def first(passkeys: PasskeyApprovals, _c: RegistryControl) -> None:
        await enrol(passkeys, SoftAuthenticator())

    session(test_db, first)

    async def without_code(passkeys: PasskeyApprovals, _c: RegistryControl) -> tuple[object, str]:
        with pytest.raises(PasskeyError) as closed:
            await passkeys.enrol_options(CODE)
        return await passkeys.status(), closed.value.code

    assert session(test_db, without_code, code=None) == ((True, False), "passkey.enrol_closed")


def test_the_database_keeps_one_active_passkey_and_revoked_ones_stay_revoked(test_db: ScratchDb) -> None:
    first = PasskeyRow(b"a" * 16, b"k" * 64, 0, "user:chairman", NOW)
    second = PasskeyRow(b"b" * 16, b"k" * 64, 0, "user:chairman", NOW)

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            rows = PasskeyRows(db.scoped(TENANT))
            assert await rows.add_first(first)
            assert not await rows.add_first(second)  # one active only
            assert await rows.replace(second)
            active = await rows.active()
            assert active is not None
            assert active.credential_id == b"b" * 16
            assert not await rows.record_use(b"a" * 16, 5, NOW)  # the revoked one is dead
            assert not await rows.replace(second)  # the same credential again: nothing changes
            assert (await rows.active()) == active
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO agentzaaheen.passkeys (tenant_id, credential_id, public_key, sign_count,"
                    " created_by, created_at) VALUES ($1, $2, $3, 0, 'user:chairman', now())",
                    TENANT, b"c" * 16, b"k" * 64,
                )  # fmt: skip
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE agentzaaheen.passkeys SET revoked_at = NULL WHERE credential_id = $1", b"a" * 16
                )
        finally:
            await conn.close()

    run(body())


def test_a_bad_set_up_answer_is_refused_and_audited(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> tuple[str, str, object]:
        options = await passkeys.enrol_options(CODE)
        with pytest.raises(PasskeyError) as unverified:
            await passkeys.enrol(CODE, device.register(options, verified=False))
        with pytest.raises(PasskeyError) as wrong:
            await passkeys.enrol("ffffffff", device.register(await passkeys.enrol_options(CODE)))
        return unverified.value.code, wrong.value.code, await passkeys.status()

    assert session(test_db, use) == ("passkey.not_verified", "passkey.wrong_code", (False, True))
    assert [row[1:] for row in audit(test_db)] == [
        ("deny", "passkey.not_verified"),
        ("deny", "passkey.wrong_code"),
    ]


def test_the_same_passkey_cannot_be_set_up_twice(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def first(passkeys: PasskeyApprovals, _c: RegistryControl) -> None:
        await enrol(passkeys, device)

    session(test_db, first)

    async def again(passkeys: PasskeyApprovals, _c: RegistryControl) -> str:
        with pytest.raises(PasskeyError) as caught:
            await enrol(passkeys, device)  # the very same credential as the active one
        return caught.value.code

    assert session(test_db, again) == "passkey.not_saved"


def test_a_counter_that_cannot_move_forward_fails_the_approval(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, control: RegistryControl) -> tuple[Verdict, State]:
        await enrol(passkeys, device)
        request_id = await asked(control)
        options = await passkeys.approve_options(request_id)

        async def raced(*_args: object) -> bool:  # another approval moved the counter past us
            return False

        passkeys._rows.record_use = raced  # type: ignore[method-assign,assignment]
        verdict = await passkeys.approve(request_id, device.sign(options))
        return verdict, (await control.standing()).states[(Kind.SKILL, SKILL)]

    assert session(test_db, use) == (Verdict.PASSKEY_FAILED, State.OFF)
    assert audit(test_db)[-1] == ("registry.approve", "deny", "passkey.counter_went_back")


def test_a_request_decided_in_between_is_not_approved(test_db: ScratchDb) -> None:
    async def use(_p: PasskeyApprovals, control: RegistryControl) -> Verdict:
        request_id = await asked(control)
        real = control.approvable

        async def then_withdrawn(rid: UUID) -> Verdict:
            verdict = await real(rid)
            await control.withdraw(rid, now=NOW, **ME)  # type: ignore[arg-type]
            return verdict

        control.approvable = then_withdrawn  # type: ignore[method-assign,assignment]
        return await control.approve(request_id, step_up_at=NOW, now=NOW, **ME)  # type: ignore[arg-type]

    assert session(test_db, use) is Verdict.UNKNOWN_REQUEST


def test_only_the_chairman_may_refuse(test_db: ScratchDb) -> None:
    async def use(_p: PasskeyApprovals, control: RegistryControl) -> tuple[Verdict, int]:
        request_id = await asked(control)
        verdict = await control.refuse(request_id, role=Role.AI_OPERATOR, channel=Channel.MCP, now=NOW)
        return verdict, len((await control.standing()).pending)

    assert session(test_db, use) == (Verdict.NOT_ALLOWED, 1)


def test_a_request_for_an_outside_bundle_can_never_be_approved(test_db: ScratchDb) -> None:
    """Asking for one is refused already; this guards a row put there some other way."""
    request_id = uuid4()

    async def plant() -> None:
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            await conn.execute(
                "INSERT INTO agentzaaheen.registry_requests (id, tenant_id, kind, item_id,"
                " fingerprint, status,"
                " requested_by, requested_at) VALUES ($1, $2, 'plugin', 'outside', $3, 'pending',"
                " 'user:chairman', now())",
                request_id, TENANT, FILES,
            )  # fmt: skip
        finally:
            await conn.close()

    run(plant())

    async def use(_p: PasskeyApprovals, control: RegistryControl) -> Verdict:
        return await control.approvable(request_id)

    assert session(test_db, use) is Verdict.VIEW_ONLY


def test_the_store_does_not_approve_a_request_that_is_not_pending(test_db: ScratchDb) -> None:
    async def use(_p: PasskeyApprovals, control: RegistryControl) -> bool:
        request_id = await asked(control)
        await control.withdraw(request_id, now=NOW, **ME)  # type: ignore[arg-type]
        return await control._rows.approve(request_id, by="user:chairman", at=NOW, step_up_at=NOW)

    assert session(test_db, use) is False


# ---- signing in to the dashboard (ADR-SEC-010)


def test_signing_in_needs_a_good_signature_for_the_sign_in_challenge_and_is_audited(
    test_db: ScratchDb,
) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> list[bool]:
        await enrol(passkeys, device)
        options = await passkeys.sign_in_options()
        good = device.sign(options)
        first = await passkeys.sign_in(good)
        replayed = await passkeys.sign_in(good)  # the challenge was used: never twice
        stranger = await passkeys.sign_in(SoftAuthenticator().sign(await passkeys.sign_in_options()))
        await passkeys.signed_out("session.by_button")
        return [first, replayed, stranger]

    assert session(test_db, use) == [True, False, False]
    assert audit(test_db)[-4:] == [
        ("session.sign_in", "allow", "session.signed_in"),
        ("session.sign_in", "deny", "passkey.no_challenge"),
        ("session.sign_in", "deny", "passkey.unknown"),
        ("session.sign_out", "allow", "session.by_button"),
    ]


def test_without_a_passkey_no_sign_in_is_offered(test_db: ScratchDb) -> None:
    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> str:
        with pytest.raises(PasskeyError) as caught:
            await passkeys.sign_in_options()
        return caught.value.code

    assert session(test_db, use) == "registry.no_passkey"


def test_a_counter_that_cannot_move_forward_fails_the_sign_in(test_db: ScratchDb) -> None:
    device = SoftAuthenticator()

    async def use(passkeys: PasskeyApprovals, _c: RegistryControl) -> bool:
        await enrol(passkeys, device)
        options = await passkeys.sign_in_options()

        async def raced(*_args: object) -> bool:  # another signature moved the counter past us
            return False

        passkeys._rows.record_use = raced  # type: ignore[method-assign,assignment]
        return await passkeys.sign_in(device.sign(options))

    assert session(test_db, use) is False
    assert audit(test_db)[-1] == ("session.sign_in", "deny", "passkey.counter_went_back")
