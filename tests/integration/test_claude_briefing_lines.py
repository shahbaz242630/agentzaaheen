"""The laptop's Claude lines for the briefing, read from a real laptop database (F10 slice 3a).

Runs that did not run in the day before the briefing are named; older ones are
not; a cap at 80% or more this month is said.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from agentzaaheen.core.store import Database, DatabaseSettings, MissRow, ModelCallMisses, ModelCalls
from tests.integration.conftest import run
from tests.integration.test_dashboard_switches import _switches, laptop_url
from tools.laptop_db import LAPTOP_TENANT

__all__ = ["laptop_url"]  # the pytest fixture shared with the switches tests

BRIEFING = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)  # 07:00 in Dubai


def miss(at: datetime, role: str) -> MissRow:
    return MissRow(at, role, "heads", "claude-opus-5-5", "model.unreachable", "net.timeout", "run-1")


def test_the_last_days_misses_and_the_caps_at_80_percent_are_said(laptop_url: SecretStr) -> None:
    async def body() -> tuple[str, ...]:
        db = await Database.open(DatabaseSettings(dsn=laptop_url))
        try:
            board = db.scoped(LAPTOP_TENANT)
            await ModelCallMisses(board).add(miss(BRIEFING - timedelta(hours=3), "head-of-engineering"))
            await ModelCallMisses(board).add(
                miss(BRIEFING - timedelta(days=2), "old-role")
            )  # before the window
            await ModelCalls(board).add("code-review", "claude-sonnet-5-5", 16_500_000, at=BRIEFING)
        finally:
            await db.close()
        switches = _switches(laptop_url)
        try:
            return await switches.claude_lines(BRIEFING)
        finally:
            await switches.close()

    assert run(body()) == (
        "1 Claude run did not run (head-of-engineering: Claude could not be reached (it timed out))."
        " Nothing answered in their place.",
        "Claude spend: code review at 82% of its USD 20 cap this month.",
    )
