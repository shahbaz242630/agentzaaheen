"""Registry switches through the policy layer (ADR-030), against a real database.

Off works at once; "on" can only be asked for; changed files undo an approval;
outside bundles are view only; every attempt, refusals included, is audited and
the audit chain still verifies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.integration.conftest import ScratchDb, open_db, run
from yaqzan.core.policy import Channel, Ledger, RegistryControl, Role, Standing, Verdict
from yaqzan.core.registry import Connector, Kind, Layout, Plugin, Registry, Skill, State
from yaqzan.core.registry.models import Invocation
from yaqzan.core.sdk import ToolLevel

TENANT = uuid4()
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
FILES = "a" * 64
CHANGED = "b" * 64
ME = {"role": Role.CHAIRMAN, "channel": Channel.DASHBOARD, "now": NOW}


def registry(skill_files: str = FILES) -> Registry:
    return Registry(
        connectors=(
            Connector(
                id="github", name="GitHub", category="Code", description="Reads counts.", version="0.2.0",
                level=ToolLevel.READ, hosts=("api.github.com",), endpoints=("GET /x",),
                key_slots=("github_token",), fingerprint=FILES,
            ),
        ),
        skills=(
            Skill(
                id="triage-failing-build", description="Explain a red build.", invocation=Invocation.AUTO,
                requires=("github",), fingerprint=skill_files,
            ),
        ),
        plugins=(
            Plugin(
                id="outside", description="From elsewhere.", layout=Layout.CLAUDE, skills=1, connectors=0,
                commands=0, agents=0, hooks=False, fingerprint=FILES,
            ),
        ),
    )  # fmt: skip


def with_control[T](
    test_db: ScratchDb, use: Callable[[RegistryControl], Awaitable[T]], reg: Registry | None = None
) -> T:
    async def body() -> T:
        async with open_db(test_db.app_url) as db:
            return await use(RegistryControl(db, TENANT, reg or registry()))

    return run(body())


def audit(test_db: ScratchDb) -> list[tuple[str, str, str, str | None]]:
    async def body() -> list[tuple[str, str, str, str | None]]:
        async with open_db(test_db.app_url) as db:
            ledger = Ledger(db, TENANT)
            assert (await ledger.verify()).ok
            conn = await asyncpg.connect(test_db.app_url)
            try:
                rows = await conn.fetch(
                    "SELECT action, outcome, reason_code, target_ref FROM yaqzan.audit_log"
                    " WHERE tenant_id = $1 ORDER BY seq",
                    TENANT,
                )
            finally:
                await conn.close()
            return [(r["action"], r["outcome"], r["reason_code"], r["target_ref"]) for r in rows]

    return run(body())


def test_our_items_start_on_and_outside_bundles_are_view_only(test_db: ScratchDb) -> None:
    async def use(control: RegistryControl) -> Standing:
        return await control.standing()

    standing = with_control(test_db, use)
    assert standing.states == {
        (Kind.CONNECTOR, "github"): State.ON,
        (Kind.SKILL, "triage-failing-build"): State.ON,
        (Kind.PLUGIN, "outside"): State.VIEW_ONLY,
    }
    assert standing.pending == ()
    assert standing.on(Kind.SKILL) == {"triage-failing-build"}
    assert standing.on(Kind.PLUGIN) == frozenset()


def test_switch_off_works_at_once_and_only_once(test_db: ScratchDb) -> None:
    async def use(control: RegistryControl) -> tuple[Verdict, Verdict, Standing]:
        first = await control.switch_off(Kind.SKILL, "triage-failing-build", **ME)  # type: ignore[arg-type]
        again = await control.switch_off(Kind.SKILL, "triage-failing-build", **ME)  # type: ignore[arg-type]
        return first, again, await control.standing()

    first, again, standing = with_control(test_db, use)
    assert (first, again) == (Verdict.DONE, Verdict.NOT_ON)
    assert standing.states[(Kind.SKILL, "triage-failing-build")] is State.OFF
    assert standing.on(Kind.SKILL) == frozenset()
    assert standing.on(Kind.CONNECTOR) == {"github"}
    assert audit(test_db) == [
        ("registry.switch_off", "allow", "registry.done", "skill:triage-failing-build"),
        ("registry.switch_off", "deny", "registry.not_on", "skill:triage-failing-build"),
    ]


def test_asking_to_switch_on_files_a_request_and_switches_nothing_on(test_db: ScratchDb) -> None:
    async def use(control: RegistryControl) -> tuple[Verdict, Verdict, Verdict, Standing]:
        on_already = await control.request_on(Kind.CONNECTOR, "github", **ME)  # type: ignore[arg-type]
        await control.switch_off(Kind.CONNECTOR, "github", **ME)  # type: ignore[arg-type]
        asked = await control.request_on(Kind.CONNECTOR, "github", **ME)  # type: ignore[arg-type]
        twice = await control.request_on(Kind.CONNECTOR, "github", **ME)  # type: ignore[arg-type]
        return on_already, asked, twice, await control.standing()

    on_already, asked, twice, standing = with_control(test_db, use)
    assert (on_already, asked, twice) == (Verdict.NOT_OFF, Verdict.DONE, Verdict.ALREADY_ASKED)
    assert standing.states[(Kind.CONNECTOR, "github")] is State.OFF  # still off: only approval turns it on
    (pending,) = standing.pending
    assert (pending.kind, pending.item_id, pending.stale) == (Kind.CONNECTOR, "github", False)
    reasons = [row[2] for row in audit(test_db) if row[0] == "registry.request_on"]
    assert reasons == ["registry.not_off", "registry.done", "registry.already_asked"]


def test_a_request_goes_stale_when_the_files_change(test_db: ScratchDb) -> None:
    async def ask(control: RegistryControl) -> None:
        await control.switch_off(Kind.SKILL, "triage-failing-build", **ME)  # type: ignore[arg-type]
        await control.request_on(Kind.SKILL, "triage-failing-build", **ME)  # type: ignore[arg-type]

    async def look(control: RegistryControl) -> Standing:
        return await control.standing()

    with_control(test_db, ask)
    (pending,) = with_control(test_db, look, registry(skill_files=CHANGED)).pending
    assert pending.stale


def test_withdraw_a_request(test_db: ScratchDb) -> None:
    async def use(control: RegistryControl) -> tuple[Verdict, Verdict, Verdict, Standing]:
        await control.switch_off(Kind.SKILL, "triage-failing-build", **ME)  # type: ignore[arg-type]
        await control.request_on(Kind.SKILL, "triage-failing-build", **ME)  # type: ignore[arg-type]
        (pending,) = (await control.standing()).pending
        first = await control.withdraw(pending.id, **ME)  # type: ignore[arg-type]
        again = await control.withdraw(pending.id, **ME)  # type: ignore[arg-type]
        unknown = await control.withdraw(uuid4(), **ME)  # type: ignore[arg-type]
        return first, again, unknown, await control.standing()

    first, again, unknown, standing = with_control(test_db, use)
    assert (first, again, unknown) == (Verdict.DONE, Verdict.UNKNOWN_REQUEST, Verdict.UNKNOWN_REQUEST)
    assert standing.pending == ()


@pytest.mark.parametrize(
    ("kind", "item", "expected"),
    [
        (Kind.SKILL, "no-such-skill", Verdict.UNKNOWN_ITEM),
        (Kind.CONNECTOR, "triage-failing-build", Verdict.UNKNOWN_ITEM),  # right id, wrong kind
        (Kind.PLUGIN, "outside", Verdict.VIEW_ONLY),
    ],
)
def test_unknown_items_and_outside_bundles_are_refused(
    test_db: ScratchDb, kind: Kind, item: str, expected: Verdict
) -> None:
    async def use(control: RegistryControl) -> tuple[Verdict, Verdict]:
        off = await control.switch_off(kind, item, **ME)  # type: ignore[arg-type]
        ask = await control.request_on(kind, item, **ME)  # type: ignore[arg-type]
        return off, ask

    assert with_control(test_db, use) == (expected, expected)
    assert [row[1] for row in audit(test_db)] == ["deny", "deny"]


@pytest.mark.parametrize(
    ("role", "channel"),
    [
        (Role.AI_OPERATOR, Channel.MCP),  # Claude may only read
        (Role.VIEWER, Channel.DASHBOARD),
        (Role.CHAIRMAN, Channel.MCP),  # not a channel the Chairman uses
    ],
)
def test_only_the_chairman_may_switch_ask_or_withdraw(
    test_db: ScratchDb, role: Role, channel: Channel
) -> None:
    who = {"role": role, "channel": channel, "now": NOW}
    request_id: list[UUID] = []

    async def use(control: RegistryControl) -> tuple[Verdict, Verdict, Verdict, Standing]:
        await control.switch_off(Kind.CONNECTOR, "github", **ME)  # type: ignore[arg-type]
        await control.request_on(Kind.CONNECTOR, "github", **ME)  # type: ignore[arg-type]
        request_id.append((await control.standing()).pending[0].id)
        off = await control.switch_off(Kind.SKILL, "triage-failing-build", **who)  # type: ignore[arg-type]
        ask = await control.request_on(Kind.CONNECTOR, "github", **who)  # type: ignore[arg-type]
        withdraw = await control.withdraw(request_id[0], **who)  # type: ignore[arg-type]
        return off, ask, withdraw, await control.standing()

    off, ask, withdraw, standing = with_control(test_db, use)
    assert (off, ask, withdraw) == (Verdict.NOT_ALLOWED,) * 3
    assert standing.states[(Kind.SKILL, "triage-failing-build")] is State.ON
    assert [p.id for p in standing.pending] == request_id
    assert [row[1] for row in audit(test_db)[2:]] == ["deny", "deny", "deny"]


def test_an_approval_lapses_when_the_files_change(test_db: ScratchDb) -> None:
    """Stored "on" counts only for the approved files (approving itself arrives in slice 2c)."""
    approved = uuid4()

    async def approve() -> None:
        conn = await asyncpg.connect(test_db.app_url)
        try:
            await conn.execute(
                "INSERT INTO yaqzan.registry_requests (id, tenant_id, kind, item_id, fingerprint, status,"
                " requested_by, requested_at, decided_by, decided_at, step_up_at)"
                " VALUES ($1, $2, 'skill', 'triage-failing-build', $3, 'approved', 'user:chairman', now(),"
                " 'user:chairman', now(), now())",
                approved, TENANT, FILES,
            )  # fmt: skip
            await conn.execute(
                "INSERT INTO yaqzan.registry_switches (tenant_id, kind, item_id, enabled, fingerprint,"
                " request_id, changed_by, changed_at)"
                " VALUES ($1, 'skill', 'triage-failing-build', true, $2, $3, 'user:chairman', now())",
                TENANT, FILES, approved,
            )  # fmt: skip
        finally:
            await conn.close()

    async def skill_state(control: RegistryControl) -> State:
        return (await control.standing()).states[(Kind.SKILL, "triage-failing-build")]

    run(approve())
    assert with_control(test_db, skill_state) is State.ON
    assert with_control(test_db, skill_state, registry(skill_files=CHANGED)) is State.CHANGED
