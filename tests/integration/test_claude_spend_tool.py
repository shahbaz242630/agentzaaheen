"""The Claude spend report against a real Postgres (F10 slice 3b): spend, misses and the thinking estimate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from agentzaaheen.core.store import CallTokens, MissRow, ModelCallMisses, ModelCalls
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tools import claude_spend
from tools.laptop_db import LAPTOP_TENANT

NOW = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
OPUS = "claude-opus-5-5"


def test_the_report_says_spend_misses_and_the_thinking_estimate(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, LAPTOP_TENANT))

    async def body() -> list[str]:
        async with open_db(test_db.app_url) as db:
            board = db.scoped(LAPTOP_TENANT)
            counts = CallTokens(500, 1_000, cache_read=0, cache_write=0, answer_chars=400)
            await ModelCalls(board).add("heads", OPUS, 48_000_000, at=NOW - timedelta(hours=1), tokens=counts)
            for hours_ago in (2, 30):  # only the first is in the last day
                await ModelCallMisses(board).add(
                    MissRow(
                        NOW - timedelta(hours=hours_ago), "sales", "heads", OPUS, "model.declined", None, "r1"
                    )
                )
        return await claude_spend.lines(SecretStr(test_db.app_url), NOW)

    assert run(body()) == [
        "Claude spend, 2026-10 (UTC):",
        "  department heads: USD 48.0000 of 60",
        "  code review: USD 0.0000 of 20",
        "  computer use: USD 0.0000 of 20",
        "  Board chat: USD 0.0000 of 80",
        "  all Claude use: USD 48.0000 of the 180 stop",
        "Last day:",
        "  1 Claude run did not run (sales: Claude declined to answer). Nothing answered in their place.",
        "  Claude spend: department heads at 80% of its USD 60 cap this month.",
        "Opus thinking (estimate, first 1 of 20 calls): 1000 output tokens a call, about 100 of them visible,"
        " so about 900 thinking (about USD 0.0180 a call).",
        "  Not enough calls to decide yet: the decision waits for 20.",
    ]


def test_without_the_laptop_database_it_says_how_to_start_it(capsys: pytest.CaptureFixture[str]) -> None:
    async def stopped() -> SecretStr | None:
        return None

    assert claude_spend.main([], prepare=stopped) == 1
    assert "Start it: python -m tools.laptop_db up" in capsys.readouterr().out


def test_a_quiet_month_says_so_and_twenty_opus_calls_are_enough_to_decide(
    test_db: ScratchDb, capsys: pytest.CaptureFixture[str]
) -> None:
    run(add_workspaces(test_db.app_url, LAPTOP_TENANT))

    async def the_test_db() -> SecretStr | None:
        return SecretStr(test_db.app_url)

    assert claude_spend.main([], prepare=the_test_db, clock=lambda: NOW) == 0
    quiet = capsys.readouterr().out
    assert "Last day: every Claude run ran; every cap is under 80%." in quiet
    assert "Opus thinking: no Opus call with counts yet." in quiet

    async def twenty() -> None:
        async with open_db(test_db.app_url) as db:
            calls = ModelCalls(db.scoped(LAPTOP_TENANT))
            counts = CallTokens(500, 400, cache_read=0, cache_write=0, answer_chars=400)
            for minute in range(20):
                await calls.add("heads", OPUS, 1, at=NOW - timedelta(minutes=minute), tokens=counts)

    run(twenty())
    assert claude_spend.main([], prepare=the_test_db, clock=lambda: NOW) == 0
    decided = capsys.readouterr().out
    assert "first 20 of 20 calls" in decided
    assert "Not enough calls" not in decided
