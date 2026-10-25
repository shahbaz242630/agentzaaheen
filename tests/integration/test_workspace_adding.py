"""Adding a project against a real Postgres (F17 slice 5a, ADR-039 amended 24 Oct).

A new workspace and its GitHub repository are added together, behind the
walls, with no step-up: nothing in a new workspace is switched on or can
spend. Its address comes from its name and is never reused; a repository
belongs to at most one workspace of a tenant; every attempt is audited.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import asyncpg
import pytest

from agentzaaheen.core.policy import Channel, LifecycleVerdict, Role, WorkspaceControl, slug_of
from agentzaaheen.core.store import (
    AgentRows,
    AuditRows,
    Database,
    StoreError,
    StoreErrorCode,
    WorkspaceRow,
    WorkspaceRows,
    WorkspaceSources,
)
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_policy_agents import ME

TENANT = UUID("00000000-0000-4000-8000-0000000000a1")
OTHER_TENANT = UUID("00000000-0000-4000-8000-0000000000a2")
EXISTING = UUID("00000000-0000-4000-8000-0000000000a3")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EXISTING))
    run(add_workspaces(test_db.app_url, OTHER_TENANT))


def rules(db: Database, tenant: UUID = TENANT) -> WorkspaceControl:
    return WorkspaceControl(db, tenant, lambda: NOW)


def test_a_new_project_gets_its_workspace_and_repository_and_nothing_switched_on(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            added = await rules(db).add(
                "  Sandoq   Kin ",
                github="shahbaz242630/Document-Vault",
                role=Role.CHAIRMAN,
                channel=Channel.DASHBOARD,
            )
            assert added.verdict is LifecycleVerdict.DONE
            new = added.workspace
            assert new is not None
            assert (new.slug, new.name) == ("sandoq-kin", "Sandoq Kin")
            assert new in await rules(db).active()
            here = db.scoped(TENANT, new.workspace_id)
            assert await WorkspaceSources(here).github() == "shahbaz242630/Document-Vault"
            assert await AgentRows(here).agents() == []
            assert await AgentRows(here).settings() is None  # no switch, no cap: nothing may spend
            assert await rules(db).repos() == {"sandoq-kin": "shahbaz242630/Document-Vault"}
            audit = await AuditRows(db.scoped(TENANT)).all_rows()
        (entry,) = [e for e in audit if e.action == "workspaces.add"]
        assert (entry.outcome, entry.reason_code, entry.project_id) == (
            "allow",
            "workspaces.done",
            new.workspace_id,
        )
        assert entry.details == {
            "verdict": "workspaces.done",
            "slug": "sandoq-kin",
            "repo": "shahbaz242630/Document-Vault",
        }

    run(body())


def test_a_project_may_be_added_without_a_repository(test_db: ScratchDb) -> None:
    async def body() -> str | None:
        async with open_db(test_db.app_url) as db:
            added = await rules(db).add("Notes", github=None, **ME)
            assert added.workspace is not None
            return await WorkspaceSources(db.scoped(TENANT, added.workspace.workspace_id)).github()

    assert run(body()) is None


def test_names_and_repositories_are_never_shared(test_db: ScratchDb) -> None:
    async def body() -> list[LifecycleVerdict]:
        async with open_db(test_db.app_url) as db:
            add = rules(db).add
            return [
                (await add("Shop", github="owner/shop", **ME)).verdict,
                (await add("shop!", github="owner/other", **ME)).verdict,  # same address
                (await add("Shop Two", github="OWNER/Shop", **ME)).verdict,  # same repo
                (await add(f"w {EXISTING.hex}", github=None, **ME)).verdict,
            ]

    assert run(body()) == [
        LifecycleVerdict.DONE,
        LifecycleVerdict.NAME_TAKEN,
        LifecycleVerdict.REPO_TAKEN,
        LifecycleVerdict.NAME_TAKEN,  # the existing workspace's address
    ]


def test_another_tenant_may_use_the_same_name_and_repository(test_db: ScratchDb) -> None:
    async def body() -> list[LifecycleVerdict]:
        async with open_db(test_db.app_url) as db:
            mine = await rules(db).add("Shop", github="owner/shop", **ME)
            theirs = await rules(db, OTHER_TENANT).add("Shop", github="owner/shop", **ME)
            return [mine.verdict, theirs.verdict]

    assert run(body()) == [LifecycleVerdict.DONE, LifecycleVerdict.DONE]


@pytest.mark.parametrize(
    ("name", "github", "verdict"),
    [
        ("", None, LifecycleVerdict.BAD_NAME),
        ("!!!", None, LifecycleVerdict.DONE),  # no letters: it gets a made-up address
        ("نور", None, LifecycleVerdict.DONE),
        ("x" * 81, None, LifecycleVerdict.BAD_NAME),
        ("Tab\there", None, LifecycleVerdict.DONE),  # spaces of any kind become one
        ("Bell\x07", None, LifecycleVerdict.BAD_NAME),
        ("Shop", "no-slash", LifecycleVerdict.BAD_REPO),
        ("Shop", "owner/..", LifecycleVerdict.BAD_REPO),
        ("Shop", "owner/a b", LifecycleVerdict.BAD_REPO),
    ],
)
def test_a_name_or_repository_that_breaks_a_rule_is_refused(
    test_db: ScratchDb, name: str, github: str | None, verdict: LifecycleVerdict
) -> None:
    async def body() -> LifecycleVerdict:
        async with open_db(test_db.app_url) as db:
            return (await rules(db).add(name, github=github, **ME)).verdict

    assert run(body()) is verdict


@pytest.mark.parametrize(
    ("role", "channel"),
    [(Role.VIEWER, Channel.DASHBOARD), (Role.AI_OPERATOR, Channel.MCP), (Role.CHAIRMAN, Channel.SYSTEM)],
)
def test_only_the_chairman_adds_a_project(test_db: ScratchDb, role: Role, channel: Channel) -> None:
    async def body() -> tuple[LifecycleVerdict, int]:
        async with open_db(test_db.app_url) as db:
            verdict = (await rules(db).add("Shop", github="owner/shop", role=role, channel=channel)).verdict
            return verdict, len(await rules(db).active())

    assert run(body()) == (LifecycleVerdict.NOT_ALLOWED, 1)


def test_an_archived_project_keeps_its_name_and_repository(test_db: ScratchDb) -> None:
    async def body() -> list[LifecycleVerdict]:
        async with open_db(test_db.app_url) as db:
            shop = (await rules(db).add("Shop", github="owner/shop", **ME)).workspace
            assert shop is not None
            await rules(db).archive(shop.workspace_id, **ME, step_up_at=NOW)
            return [
                (await rules(db).add("Shop", github=None, **ME)).verdict,
                (await rules(db).add("New shop", github="owner/shop", **ME)).verdict,
            ]

    assert run(body()) == [LifecycleVerdict.NAME_TAKEN, LifecycleVerdict.REPO_TAKEN]


def test_deleting_for_good_frees_the_repository_but_never_the_name(test_db: ScratchDb) -> None:
    async def body() -> list[LifecycleVerdict]:
        async with open_db(test_db.app_url) as db:
            shop = (await rules(db).add("Shop", github="owner/shop", **ME)).workspace
            assert shop is not None
            await rules(db).archive(shop.workspace_id, **ME, step_up_at=NOW)
            later = WorkspaceControl(db, TENANT, lambda: NOW.replace(second=1))
            assert await later.erase(shop.workspace_id, **ME, step_up_at=NOW.replace(second=1)) is (
                LifecycleVerdict.DONE
            )
            return [
                (await rules(db).add("Shop", github=None, **ME)).verdict,
                (await rules(db).add("Shop again", github="owner/shop", **ME)).verdict,
            ]

    assert run(body()) == [LifecycleVerdict.NAME_TAKEN, LifecycleVerdict.DONE]


def test_the_database_itself_refuses_a_shared_repository_and_a_second_link(test_db: ScratchDb) -> None:
    async def body() -> list[str]:
        async with open_db(test_db.app_url) as db:
            shop = (await rules(db).add("Shop", github="owner/shop", **ME)).workspace
            assert shop is not None
        refused = []
        for workspace, repo in [(EXISTING, "Owner/SHOP"), (shop.workspace_id, "owner/second")]:
            conn = await connect_as(test_db.app_url, TENANT, workspace)
            try:
                await conn.execute(
                    "INSERT INTO agentzaaheen.workspace_sources"
                    " (tenant_id, project_id, kind, ref, added_by, added_at)"
                    " VALUES ($1, $2, 'github', $3, 'user:chairman', $4)",
                    TENANT,
                    workspace,
                    repo,
                    NOW,
                )
            except asyncpg.UniqueViolationError as exc:
                refused.append(type(exc).__name__)
            finally:
                await conn.close()
        return refused

    assert run(body()) == ["UniqueViolationError", "UniqueViolationError"]


def test_a_workspace_is_created_only_through_its_own_handle(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            row = WorkspaceRow(UUID("00000000-0000-4000-8000-0000000000a9"), "x", "X")
            with pytest.raises(ValueError, match="handle for that workspace"):
                await WorkspaceRows(db.scoped(TENANT)).create(row, github=None, by="user:chairman", at=NOW)

    run(body())


def test_the_address_is_made_from_the_name() -> None:
    assert slug_of("Sandoq Kin") == "sandoq-kin"
    assert slug_of("Zaaheen (API) v2!") == "zaaheen-api-v2"
    assert slug_of("نور") == ""  # no Latin letters: add() makes up an address
    assert len(slug_of("word " * 30)) <= 40
    assert slug_of("a" * 60) == "a" * 40


def test_a_name_or_repository_taken_in_between_is_still_refused_by_the_database(
    test_db: ScratchDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> list[LifecycleVerdict]:
        async with open_db(test_db.app_url) as db:
            control = rules(db)
            await control.add("Shop", github="owner/shop", **ME)

            async def blind() -> dict[str, str]:  # the check before saving misses the other workspace
                return {}

            monkeypatch.setattr(control, "repos", blind)
            repo = (await control.add("Shop two", github="owner/shop", **ME)).verdict
            real = WorkspaceRows.slugs
            seen: list[int] = []

            async def first_blind(self: WorkspaceRows) -> set[str]:
                seen.append(1)
                return set() if len(seen) == 1 else await real(self)

            monkeypatch.setattr(WorkspaceRows, "slugs", first_blind)
            name = (await control.add("Shop", github=None, **ME)).verdict
            return [repo, name]

    assert run(body()) == [LifecycleVerdict.REPO_TAKEN, LifecycleVerdict.NAME_TAKEN]


def test_a_database_that_fails_while_adding_is_not_called_taken(
    test_db: ScratchDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    monkeypatch.setattr(WorkspaceRows, "create", broken)

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await rules(db).add("Shop", github=None, **ME)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.UNAVAILABLE


def test_adding_what_is_already_there_logs_that_nothing_needed_writing(
    test_db: ScratchDb, capsys: pytest.CaptureFixture[str]
) -> None:
    async def body() -> list[bool]:
        async with open_db(test_db.app_url) as db:
            rows = WorkspaceRows(db.scoped(TENANT))
            capsys.readouterr()  # only this test's lines
            await rows.add_tenant("Test tenant")
            existing = WorkspaceRow(workspace_id=EXISTING, slug=f"w-{EXISTING.hex}", name="Renamed?")
            fresh = WorkspaceRow(workspace_id=UUID(int=7), slug="fresh", name="Fresh")
            return [await rows.add(existing), await rows.add(fresh)]

    assert run(body()) == [False, True]
    events = [json.loads(raw) for raw in capsys.readouterr().err.splitlines() if raw.startswith("{")]
    said = [(e["event"], e["record"]) for e in events if e.get("event", "").startswith("store_")]
    assert said == [
        ("store_unchanged", "a tenant"),
        ("store_unchanged", "a workspace"),
        ("store_saved", "a workspace"),
    ]
