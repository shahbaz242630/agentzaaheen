"""The Board's Claude lines for the morning briefing (F10 slice 3a). No database."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from agentzaaheen.core.api.summary import briefing
from agentzaaheen.core.llm_gateway import claude_lines
from agentzaaheen.core.llm_gateway.spend import SpendNotice
from agentzaaheen.core.store import MissRow, StoreError, StoreErrorCode
from tools.dashboard_summaries import LaptopSummaries

AT = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)


def miss(role: str, outcome: str, detail: str | None = None) -> MissRow:
    return MissRow(AT, role, "heads", "claude-opus-5-5", outcome, detail, "run-1")


def test_nothing_to_say_says_nothing() -> None:
    assert claude_lines([], []) == ()


def test_each_run_that_did_not_run_is_named_with_why() -> None:
    (line,) = claude_lines(
        [
            miss("head-of-engineering", "model.use_cap_reached"),
            miss("code-reviewer", "model.unreachable", "net.auth"),
        ],
        [],
    )
    assert line == (
        "2 Claude runs did not run (head-of-engineering: its monthly cap is reached;"
        " code-reviewer: Claude could not be reached (the key was refused)). Nothing answered in their place."
    )


def test_one_run_reads_as_one_and_more_than_three_are_counted() -> None:
    (one,) = claude_lines([miss("head-of-sales", "model.declined")], [])
    assert one.startswith("1 Claude run did not run (head-of-sales: Claude declined to answer).")
    many = [miss(f"role-{n}", "model.customer_data") for n in range(5)]
    (line,) = claude_lines(many, [])
    assert line.startswith("5 Claude runs did not run (role-0:")
    assert "role-2" in line
    assert "role-3" not in line
    assert "; and 2 more)." in line


def test_an_unknown_code_is_shown_as_itself_never_dropped() -> None:
    (line,) = claude_lines(
        [miss("ops", "model.something_new"), miss("ops", "model.unreachable", "net.odd")], []
    )
    assert "ops: model.something_new" in line
    assert "Claude could not be reached (net.odd)" in line


def test_caps_at_80_percent_name_the_use_and_the_whole_dollar_cap() -> None:
    lines = claude_lines(
        [],
        [SpendNotice("code-review", 17_000_000, 20_000_000), SpendNotice("total", 150_000_000, 180_000_000)],
    )
    assert lines == (
        "Claude spend: code review at 85% of its USD 20 cap this month.",
        "Claude spend: all Claude use at 83% of its USD 180 cap this month.",
    )


def test_the_claude_lines_come_right_after_the_count_line() -> None:
    _, lines = briefing([], day=date(2026, 10, 25), local_time="07:00", late=False, claude=("Claude line.",))
    assert lines == ("No project has a summary today.", "Claude line.", "Details on the dashboard.")


def test_claude_lines_that_cannot_be_read_are_said_not_left_out() -> None:
    async def down(_now: datetime) -> tuple[str, ...]:
        raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    job = LaptopSummaries(
        control=None,  # type: ignore[arg-type]  # not used for the Claude lines
        portfolio=None,  # type: ignore[arg-type]
        directory=None,  # type: ignore[arg-type]
        clock=lambda: AT,
        zone=None,  # type: ignore[arg-type]
        claude=down,
    )
    assert asyncio.run(job._claude_lines()) == (
        "Claude runs could not be read (store.unavailable); see the dashboard.",
    )
