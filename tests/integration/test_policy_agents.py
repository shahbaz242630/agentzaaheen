"""Agents per workspace against a real Postgres: frozen packs, switches, caps, the job's door (F17 slice 3).

The rules: a new agent starts off with no cap; going riskier (on, a first or
higher cap) needs a passkey step-up, going safer never does; no cap means no
spending; the job's door says yes only when everything is on and nothing is
used up; one workspace never sees another's agents. The database keeps the
same rules underneath the code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_store_records import run_record
from tests.unit.test_registry_roles import pack
from yaqzan.core.policy import AgentControl, AgentVerdict, Channel, Role, RunRefusal, WorkspaceAgents
from yaqzan.core.registry.roles import RoleLibrary, load_roles
from yaqzan.core.store import AgentRows, AuditRows, Records, RoleVersionRows

TENANT = UUID("00000000-0000-4000-8000-0000000000e1")
EVA = UUID("00000000-0000-4000-8000-0000000000e2")
ZAAHEEN = UUID("00000000-0000-4000-8000-0000000000e3")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


class _Me(TypedDict):
    role: Role
    channel: Channel


ME: _Me = {"role": Role.CHAIRMAN, "channel": Channel.DASHBOARD}


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN))


@pytest.fixture
def library(tmp_path: Path) -> RoleLibrary:
    pack(tmp_path, "monitoring")
    pack(tmp_path, "sales", permission="Draft")
    return load_roles(tmp_path)


def control(db: object, library: RoleLibrary) -> AgentControl:
    return AgentControl(db, TENANT, library, lambda: NOW)  # type: ignore[arg-type]


async def _given(url: str, library: RoleLibrary, workspace: UUID = EVA) -> tuple[AgentControl, UUID]:
    async with open_db(url) as db:
        rules = control(db, library)
        assert await rules.give(workspace, "monitoring", **ME) is AgentVerdict.DONE
        (agent,) = (await rules.view(workspace)).agents
        return rules, agent.agent.agent_id


def test_a_given_agent_starts_off_with_no_cap_and_a_frozen_copy_of_its_pack(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> tuple[WorkspaceAgents, AgentVerdict, AgentVerdict, dict[str, str] | None]:
        async with open_db(test_db.app_url) as db:
            rules = control(db, library)
            await rules.give(EVA, "monitoring", **ME)
            again = await rules.give(EVA, "monitoring", **ME)
            unknown = await rules.give(EVA, "ceo", **ME)
            seen = await rules.view(EVA)
            stored = await RoleVersionRows(db.scoped(TENANT)).get("monitoring", seen.agents[0].agent.version)
            return seen, again, unknown, None if stored is None else stored.files

    seen, again, unknown, files = run(body())
    (agent,) = seen.agents
    assert (agent.agent.enabled, agent.agent.cap_minor, agent.title) == (False, None, "Monitoring")
    assert (again, unknown) == (AgentVerdict.ALREADY_GIVEN, AgentVerdict.UNKNOWN_ROLE)
    assert files == library.get("monitoring").files  # type: ignore[union-attr]
    assert (seen.enabled, seen.cap_minor, seen.spent_minor) == (False, None, 0)


def test_riskier_changes_need_a_passkey_and_safer_ones_never_do(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> list[AgentVerdict]:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            rules = control(db, library)
            return [
                await rules.set_agent(EVA, agent, enabled=True, cap_minor=None, **ME),  # on: needs a passkey
                await rules.set_agent(EVA, agent, enabled=True, cap_minor=500, **ME, step_up_at=NOW),
                await rules.set_agent(EVA, agent, enabled=True, cap_minor=900, **ME),  # higher: passkey again
                await rules.set_agent(EVA, agent, enabled=True, cap_minor=100, **ME),  # lower: fine
                await rules.set_agent(
                    EVA, agent, enabled=True, cap_minor=None, **ME
                ),  # no cap = no spend: fine
                await rules.set_agent(EVA, agent, enabled=False, cap_minor=None, **ME),  # off: fine
                await rules.set_workspace(EVA, enabled=True, cap_minor=1000, **ME),  # resume: passkey
                await rules.set_workspace(EVA, enabled=True, cap_minor=1000, **ME, step_up_at=NOW),
                await rules.set_workspace(EVA, enabled=False, cap_minor=1000, **ME),  # pause: fine
            ]

    assert run(body()) == [
        AgentVerdict.NEEDS_PASSKEY,
        AgentVerdict.DONE,
        AgentVerdict.NEEDS_PASSKEY,
        AgentVerdict.DONE,
        AgentVerdict.DONE,
        AgentVerdict.DONE,
        AgentVerdict.NEEDS_PASSKEY,
        AgentVerdict.DONE,
        AgentVerdict.DONE,
    ]


def test_an_old_step_up_does_not_count(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> AgentVerdict:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            stale = NOW - timedelta(hours=1)
            return await control(db, library).set_agent(
                EVA, agent, enabled=True, cap_minor=5, **ME, step_up_at=stale
            )

    assert run(body()) is AgentVerdict.NOT_ALLOWED


def test_only_the_chairman_on_the_dashboard_may_switch_on(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> list[AgentVerdict]:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            rules = control(db, library)
            on = {"enabled": True, "cap_minor": 5, "step_up_at": NOW}
            return [
                await rules.set_agent(EVA, agent, **on, role=Role.VIEWER, channel=Channel.DASHBOARD),  # type: ignore[arg-type]
                await rules.set_agent(EVA, agent, **on, role=Role.CHAIRMAN, channel=Channel.TELEGRAM),  # type: ignore[arg-type]
            ]

    assert run(body()) == [AgentVerdict.NOT_ALLOWED, AgentVerdict.NOT_ALLOWED]


def test_the_jobs_door_says_yes_only_when_everything_is_on_and_nothing_is_used_up(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> list[RunRefusal | None]:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            rules = control(db, library)
            seen: list[RunRefusal | None] = [(await rules.may_run(EVA, uuid4())).refusal]
            seen.append((await rules.may_run(EVA, agent)).refusal)  # workspace never switched on
            await rules.set_workspace(EVA, enabled=True, cap_minor=None, **ME, step_up_at=NOW)
            seen.append((await rules.may_run(EVA, agent)).refusal)  # agent off
            await rules.set_agent(EVA, agent, enabled=True, cap_minor=None, **ME, step_up_at=NOW)
            seen.append((await rules.may_run(EVA, agent)).refusal)  # no workspace cap
            await rules.set_workspace(EVA, enabled=True, cap_minor=1000, **ME, step_up_at=NOW)
            seen.append((await rules.may_run(EVA, agent)).refusal)  # no agent cap
            await rules.set_agent(EVA, agent, enabled=True, cap_minor=300, **ME, step_up_at=NOW)
            permit = await rules.may_run(EVA, agent)
            seen.append(permit.refusal)
            assert permit.scope is not None
            assert permit.scope.workspace_id == EVA
            records = Records(db.scoped(TENANT, EVA))
            await records.add_run(run_record(project_id=EVA, tenant_id=TENANT, agent=str(agent),
                                             cost_minor_units=300, started_at=NOW))  # fmt: skip
            seen.append((await rules.may_run(EVA, agent)).refusal)  # agent cap used
            await rules.set_agent(EVA, agent, enabled=True, cap_minor=5000, **ME, step_up_at=NOW)
            await records.add_run(run_record(project_id=EVA, tenant_id=TENANT, agent="another",
                                             cost_minor_units=700, started_at=NOW))  # fmt: skip
            seen.append((await rules.may_run(EVA, agent)).refusal)  # workspace cap used (300 + 700)
            return seen

    assert run(body()) == [
        RunRefusal.NO_AGENT,
        RunRefusal.WORKSPACE_OFF,
        RunRefusal.AGENT_OFF,
        RunRefusal.NO_WORKSPACE_CAP,
        RunRefusal.NO_AGENT_CAP,
        None,
        RunRefusal.AGENT_CAP_USED,
        RunRefusal.WORKSPACE_CAP_USED,
    ]


def test_last_months_spending_does_not_count_against_this_month(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> RunRefusal | None:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            rules = control(db, library)
            await rules.set_workspace(EVA, enabled=True, cap_minor=100, **ME, step_up_at=NOW)
            await rules.set_agent(EVA, agent, enabled=True, cap_minor=100, **ME, step_up_at=NOW)
            last_month = datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
            await Records(db.scoped(TENANT, EVA)).add_run(
                run_record(
                    project_id=EVA,
                    tenant_id=TENANT,
                    agent=str(agent),
                    cost_minor_units=999,
                    started_at=last_month,
                )
            )
            return (await rules.may_run(EVA, agent)).refusal

    assert run(body()) is None


def test_one_workspace_never_sees_or_runs_another_workspaces_agent(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> tuple[int, RunRefusal | None, AgentVerdict]:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            rules = control(db, library)
            in_zaaheen = len((await rules.view(ZAAHEEN)).agents)
            run_there = (await rules.may_run(ZAAHEEN, agent)).refusal
            switch_there = await rules.set_agent(ZAAHEEN, agent, enabled=False, cap_minor=None, **ME)
            return in_zaaheen, run_there, switch_there

    assert run(body()) == (0, RunRefusal.NO_AGENT, AgentVerdict.UNKNOWN_AGENT)


def test_a_changed_pack_shows_as_newer_but_the_agent_keeps_its_frozen_copy(
    test_db: ScratchDb, tmp_path: Path
) -> None:
    folder = pack(tmp_path / "lib", "monitoring")
    before = load_roles(tmp_path / "lib")

    async def body() -> tuple[str, bool]:
        _, agent = await _given(test_db.app_url, before)
        (folder / "authority.md").write_text("# Authority\n\nNow it may restart things.\n", encoding="utf-8")
        after = load_roles(tmp_path / "lib")
        async with open_db(test_db.app_url) as db:
            (seen,) = (await control(db, after).view(EVA)).agents
            row = await AgentRows(db.scoped(TENANT, EVA)).get(agent)
            assert row is not None
            return row.version, seen.newer_version

    version, newer = run(body())
    assert version == before.get("monitoring").version  # type: ignore[union-attr]
    assert newer is True


def test_every_decision_is_audited(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> list[tuple[str, str]]:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            await control(db, library).set_agent(EVA, agent, enabled=True, cap_minor=5, **ME)
            rows = await AuditRows(db.scoped(TENANT)).all_rows()
            return [(r.action, r.reason_code) for r in rows]

    assert run(body()) == [("agents.give", "agents.done"), ("agents.set", "agents.needs_passkey")]


# ---------------------------------------------------------------- the database keeps the rules too


@pytest.mark.parametrize(
    "statement",
    [
        # Switched on without a new step-up time, by a person.
        "UPDATE yaqzan.agents SET enabled = true, changed_by = 'user:chairman'",
        # Switched on by the system, even with a step-up time.
        "UPDATE yaqzan.agents SET enabled = true, step_up_at = now(), changed_by = 'system:yaqzan'",
        # A first cap without a step-up.
        "UPDATE yaqzan.agents SET cap_minor = 100",
        # Moved to another role.
        "UPDATE yaqzan.agents SET role_id = 'sales'",
    ],
)
def test_the_database_refuses_riskier_changes_written_around_the_code(
    test_db: ScratchDb, library: RoleLibrary, statement: str
) -> None:
    run(_given(test_db.app_url, library))

    async def body() -> None:
        conn = await connect_as(test_db.app_url, TENANT, EVA)
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    with pytest.raises(asyncpg.CheckViolationError):
        run(body())


def test_the_database_lets_anyone_switch_off_or_lower_a_cap(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> None:
        _, agent = await _given(test_db.app_url, library)
        async with open_db(test_db.app_url) as db:
            await control(db, library).set_agent(
                EVA, agent, enabled=True, cap_minor=500, **ME, step_up_at=NOW
            )
        conn = await connect_as(test_db.app_url, TENANT, EVA)
        try:
            await conn.execute(
                "UPDATE yaqzan.agents SET enabled = false, cap_minor = 10, changed_by = 'system:yaqzan'"
            )
        finally:
            await conn.close()

    run(body())


def test_the_database_refuses_an_agent_created_switched_on(test_db: ScratchDb, library: RoleLibrary) -> None:
    run(_given(test_db.app_url, library))

    async def body() -> None:
        conn = await connect_as(test_db.app_url, TENANT, ZAAHEEN)
        try:
            version = await conn.fetchval("SELECT version FROM yaqzan.role_versions LIMIT 1")
            await conn.execute(
                "INSERT INTO yaqzan.agents (tenant_id, project_id, agent_id, role_id, version, enabled,"
                " model, cap_minor, created_by, created_at, changed_by, changed_at, step_up_at)"
                " VALUES ($1, $2, $3, 'monitoring', $4, true, 'local-gemma', 100, 'user:chairman', now(),"
                " 'user:chairman', now(), now())",
                TENANT, ZAAHEEN, uuid4(), version,
            )  # fmt: skip
        finally:
            await conn.close()

    with pytest.raises(asyncpg.CheckViolationError):
        run(body())
