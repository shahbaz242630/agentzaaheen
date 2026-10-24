"""Removing a project from the dashboard, signed with Windows Hello end to end (F17 slice 4).

The dashboard's removal over the real rules and a soft passkey: each move asks
for a signature, a signature covers exactly one move to one workspace, and a
reused or wrong one changes nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_passkey_approvals import CODE, SITE, Clock, enrol, registry
from yaqzan.core.api.removal import DashboardRemoval, Step, purpose
from yaqzan.core.policy import LifecycleVerdict, PasskeyApprovals, RegistryControl, WorkspaceControl
from yaqzan.core.store import WorkspaceRow

TENANT = UUID("00000000-0000-4000-8000-0000000000e1")
EVA = UUID("00000000-0000-4000-8000-0000000000e2")
ZAAHEEN = UUID("00000000-0000-4000-8000-0000000000e3")


def board[T](
    test_db: ScratchDb,
    use: Callable[[DashboardRemoval, SoftAuthenticator, Clock, dict[UUID, WorkspaceRow]], Awaitable[T]],
) -> T:
    """The dashboard's removal over the real rules and passkey, with the passkey already set up."""

    async def body() -> T:
        await add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN)
        clock, device = Clock(), SoftAuthenticator()
        async with open_db(test_db.app_url) as db:
            control = RegistryControl(db, TENANT, registry())
            passkeys = PasskeyApprovals(db, TENANT, control, SITE, enrol_code=CODE, clock=clock)
            await enrol(passkeys, device)
            removal = DashboardRemoval(WorkspaceControl(db, TENANT, clock), passkeys)
            rows = {w.workspace_id: w for w in await removal.active()}
            return await use(removal, device, clock, rows)

    return run(body())


async def _signed(
    removal: DashboardRemoval, device: SoftAuthenticator, clock: Clock, step: Step, workspace: WorkspaceRow
) -> dict[str, object]:
    clock.now += timedelta(seconds=1)
    return device.sign(await removal.options(purpose(step, workspace)))


def test_archive_bring_back_and_delete_each_take_their_own_signature(test_db: ScratchDb) -> None:
    async def use(
        removal: DashboardRemoval, device: SoftAuthenticator, clock: Clock, rows: dict[UUID, WorkspaceRow]
    ) -> list[object]:
        eva = rows[EVA]
        unsigned = (await removal.archive(eva, None)).verdict
        archived = (await removal.archive(eva, await _signed(removal, device, clock, "archive", eva))).verdict
        listed = [w.workspace_id for w in await removal.archived()]
        restore = await _signed(removal, device, clock, "restore", eva)
        restored = await removal.restore(eva, restore)
        replayed = await removal.restore(eva, restore)  # the same signature again
        again = (await removal.archive(eva, await _signed(removal, device, clock, "archive", eva))).verdict
        erased = await removal.erase(eva, await _signed(removal, device, clock, "erase", eva))
        left = [w.workspace_id for w in [*await removal.active(), *await removal.archived()]]
        return [unsigned, archived, listed, restored, replayed, again, erased, left]

    assert board(test_db, use) == [
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.DONE,
        [EVA],
        LifecycleVerdict.DONE,
        LifecycleVerdict.PASSKEY_FAILED,
        LifecycleVerdict.DONE,
        LifecycleVerdict.DONE,
        [ZAAHEEN],
    ]


def test_a_signature_for_one_move_or_workspace_does_nothing_elsewhere(test_db: ScratchDb) -> None:
    async def use(
        removal: DashboardRemoval, device: SoftAuthenticator, clock: Clock, rows: dict[UUID, WorkspaceRow]
    ) -> list[object]:
        eva, zaaheen = rows[EVA], rows[ZAAHEEN]
        for_eva = await _signed(removal, device, clock, "archive", eva)
        other = (await removal.archive(zaaheen, for_eva)).verdict
        await removal.archive(eva, await _signed(removal, device, clock, "archive", eva))
        wrong_step = await removal.erase(eva, await _signed(removal, device, clock, "restore", eva))
        unsigned = [await removal.restore(eva, None), await removal.erase(eva, None)]
        return [other, wrong_step, *unsigned, sorted(w.workspace_id for w in await removal.archived())]

    assert board(test_db, use) == [
        LifecycleVerdict.PASSKEY_FAILED,
        LifecycleVerdict.PASSKEY_FAILED,
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.NEEDS_PASSKEY,
        [EVA],
    ]
