"""Windows Hello for one exact agent change, end to end on a real database (F17 slice 3b).

A signature is asked for one change (``step:agent:<id>:on:<cap>``). It fits only
that change, only once; anything else is refused and audited, and nothing changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agentzaaheen.core.api.agents import DashboardAgents, SwitchIn, purpose
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import AgentControl, AgentVerdict, PasskeyApprovals, RegistryControl
from agentzaaheen.core.registry.roles import RoleLibrary, load_roles
from agentzaaheen.core.store import WorkspaceRow
from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_passkey_approvals import CODE, SITE, Clock, enrol, registry
from tests.unit.test_registry_roles import pack

TENANT = UUID("00000000-0000-4000-8000-0000000000d1")
EVA = WorkspaceRow(UUID("00000000-0000-4000-8000-0000000000d2"), "eva", "Eva")
ON = SwitchIn(enabled=True, cap_minor=500)


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA.workspace_id))


@pytest.fixture
def library(tmp_path: Path) -> RoleLibrary:
    pack(tmp_path, "monitoring")
    return load_roles(tmp_path)


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


def tab[T](
    test_db: ScratchDb,
    library: RoleLibrary,
    use: Callable[[DashboardAgents, PasskeyApprovals, SoftAuthenticator, Clock], Awaitable[T]],
) -> T:
    """The Agents tab over the real rules and passkey, with the passkey already set up."""

    async def body() -> T:
        clock, device = Clock(), SoftAuthenticator()
        async with open_db(test_db.app_url) as db:
            control = RegistryControl(db, TENANT, registry())
            passkeys = PasskeyApprovals(db, TENANT, control, SITE, enrol_code=CODE, clock=clock)
            await enrol(passkeys, device)
            agents = DashboardAgents(AgentControl(db, TENANT, library, clock), passkeys, library)
            return await use(agents, passkeys, device, clock)

    return run(body())


async def _agent(agents: DashboardAgents) -> UUID:
    assert await agents.give(EVA, "monitoring") is AgentVerdict.DONE
    (agent,) = (await agents.view(EVA)).agents
    return agent.id


def test_a_signature_for_exactly_this_change_switches_the_agent_on(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def use(
        agents: DashboardAgents, _: PasskeyApprovals, device: SoftAuthenticator, clock: Clock
    ) -> list[object]:
        agent = await _agent(agents)
        unsigned = await agents.set_agent(EVA, agent, ON, None)
        clock.now += timedelta(seconds=1)
        signed = device.sign(await agents.options(purpose("agent", agent, ON)))
        done = await agents.set_agent(EVA, agent, ON, signed)
        replayed = await agents.set_agent(EVA, agent, SwitchIn(enabled=True, cap_minor=900), signed)
        (seen,) = (await agents.view(EVA)).agents
        return [unsigned, done, replayed, seen.enabled, seen.cap_minor]

    assert tab(test_db, library, use) == [
        AgentVerdict.NEEDS_PASSKEY,
        AgentVerdict.DONE,
        AgentVerdict.PASSKEY_FAILED,
        True,
        500,
    ]
    assert ("passkey.step_up", "allow", "passkey.step_up_ok") in audit(test_db)


def test_a_signature_for_one_cap_cannot_set_a_higher_one(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def use(
        agents: DashboardAgents, _: PasskeyApprovals, device: SoftAuthenticator, clock: Clock
    ) -> list[object]:
        agent = await _agent(agents)
        clock.now += timedelta(seconds=1)
        signed = device.sign(await agents.options(purpose("agent", agent, ON)))
        verdict = await agents.set_agent(EVA, agent, SwitchIn(enabled=True, cap_minor=50_000), signed)
        (seen,) = (await agents.view(EVA)).agents
        return [verdict, seen.enabled, seen.cap_minor]

    assert tab(test_db, library, use) == [AgentVerdict.PASSKEY_FAILED, False, None]
    assert audit(test_db)[-1][:2] == ("passkey.step_up", "deny")  # refused before the rules are asked


def test_a_counter_going_back_fails_the_step_up(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def use(
        agents: DashboardAgents, passkeys: PasskeyApprovals, device: SoftAuthenticator, clock: Clock
    ) -> list[object]:
        agent = await _agent(agents)
        clock.now += timedelta(seconds=1)
        signed = device.sign(await agents.options(purpose("agent", agent, ON)))

        async def raced(*_args: object) -> bool:  # another signature moved the counter past us
            return False

        passkeys._rows.record_use = raced  # type: ignore[method-assign,assignment]
        verdict = await agents.set_agent(EVA, agent, ON, signed)
        (seen,) = (await agents.view(EVA)).agents
        return [verdict, seen.enabled]

    assert tab(test_db, library, use) == [AgentVerdict.PASSKEY_FAILED, False]
    assert audit(test_db)[-1] == ("passkey.step_up", "deny", "passkey.counter_went_back")


def test_the_workspace_switch_is_signed_the_same_way(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def use(
        agents: DashboardAgents, _: PasskeyApprovals, device: SoftAuthenticator, clock: Clock
    ) -> list[object]:
        unsigned = await agents.set_workspace(EVA, ON, None)
        clock.now += timedelta(seconds=1)
        signed = device.sign(await agents.options(purpose("workspace", EVA.workspace_id, ON)))
        wrong = await agents.set_workspace(EVA, SwitchIn(enabled=True, cap_minor=501), signed)
        clock.now += timedelta(seconds=1)
        signed = device.sign(await agents.options(purpose("workspace", EVA.workspace_id, ON)))
        done = await agents.set_workspace(EVA, ON, signed)
        view = await agents.view(EVA)
        return [unsigned, wrong, done, view.enabled, view.cap_minor, view.switches]

    assert tab(test_db, library, use) == [
        AgentVerdict.NEEDS_PASSKEY,
        AgentVerdict.PASSKEY_FAILED,
        AgentVerdict.DONE,
        True,
        500,
        True,
    ]


def test_step_up_options_need_a_step_purpose_and_a_passkey(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def use(
        agents: DashboardAgents, passkeys: PasskeyApprovals, _: SoftAuthenticator, __: Clock
    ) -> str:
        del agents
        with pytest.raises(PasskeyError) as caught:
            await passkeys.step_up_options("registry:approve")
        return caught.value.code

    assert tab(test_db, library, use) == "passkey.bad_purpose"


def test_without_a_passkey_no_step_up_is_offered(test_db: ScratchDb) -> None:
    async def body() -> str:
        async with open_db(test_db.app_url) as db:
            control = RegistryControl(db, TENANT, registry())
            passkeys = PasskeyApprovals(db, TENANT, control, SITE, enrol_code=None, clock=Clock())
            with pytest.raises(PasskeyError) as caught:
                await passkeys.step_up_options("step:agent:x:on:5")
            return caught.value.code

    assert run(body()) == "registry.no_passkey"


def test_the_library_is_listed_by_name(library: RoleLibrary) -> None:
    roles = DashboardAgents.roles_of(library)
    assert [r.id for r in roles] == ["monitoring"]
