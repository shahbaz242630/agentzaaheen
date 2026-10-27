"""The daily clean-up of old chats (F19 slice 1): at start, then daily; a failure never ends it."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.store import StoreError, StoreErrorCode, WorkspaceRow
from tests.unit.test_log_door_lines import lines
from tools.dashboard_chats import ChatSweep
from tools.laptop_db import LAPTOP_WORKSPACES


class Forget:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, workspaces: Sequence[WorkspaceRow]) -> int:
        self.calls.append(tuple(w.slug for w in workspaces))
        if self.fail_first and len(self.calls) == 1:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return 0


def test_sweeps_every_listed_workspace_at_start_and_again_after_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    forget = Forget(fail_first=True)

    async def body() -> None:
        sweep = ChatSweep(forget, WorkspaceDirectory(LAPTOP_WORKSPACES), every_s=0.01)
        await sweep.start()
        for _ in range(200):
            if len(forget.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        await sweep.close()

    asyncio.run(body())
    assert len(forget.calls) >= 2  # the failed first sweep did not end the sweeps
    assert forget.calls[0] == tuple(w.slug for w in LAPTOP_WORKSPACES)
    (line,) = lines(capsys, "chats_sweep_failed")
    assert line["kind"] == "StoreError"


def test_close_before_start_is_fine() -> None:
    asyncio.run(ChatSweep(Forget(), WorkspaceDirectory(LAPTOP_WORKSPACES)).close())
