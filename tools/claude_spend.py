"""What Claude cost this month, what did not run, and how much of Opus's output is thinking (F10 slice 3b).

Reads the laptop database only (never Claude, never a key) and prints:

1. this month's spend for each use against its cap, and the total against the USD 180 stop;
2. the Claude runs that did not run in the last day, each with why (the briefing's words);
3. an **estimate** of Opus's hidden thinking from the first 20 Opus calls that kept
   their counts: output tokens minus the visible answer (characters / 4). ADR-033 sets
   Opus thinking from this evidence once there are 20 calls.

Usage:
    uv run python -m tools.claude_spend
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import (
    CAPS_USD,
    MODELS,
    TOTAL_STOP_USD,
    SpendLedger,
    StoreBook,
    claude_lines,
    thinking_estimate,
)
from agentzaaheen.core.llm_gateway.model_map import usd
from agentzaaheen.core.llm_gateway.report import CAP_NAMES, THINKING_SAMPLE
from agentzaaheen.core.llm_gateway.spend import month_of
from agentzaaheen.core.sdk import ModelLevel
from agentzaaheen.core.store import Database, DatabaseSettings, ModelCallMisses, ModelCalls
from tools import laptop_db

Prepare = Callable[[], Coroutine[Any, Any, SecretStr | None]]
Clock = Callable[[], datetime]


async def lines(url: SecretStr, now: datetime) -> list[str]:
    """The report's lines, read from the laptop database at ``url``."""
    db = await Database.open(DatabaseSettings(dsn=url, min_size=1, max_size=1))
    try:
        board = db.scoped(laptop_db.LAPTOP_TENANT)
        calls = ModelCalls(board)
        book = StoreBook(calls)
        month = month_of(now)
        out = [f"Claude spend, {month} (UTC):"]
        total = 0
        for use, cap_usd in CAPS_USD.items():
            spent = await book.spent(use, month)
            total += spent
            out.append(f"  {CAP_NAMES[use.value]}: USD {usd(spent)} of {cap_usd}")
        out.append(f"  all Claude use: USD {usd(total)} of the {TOTAL_STOP_USD} stop")
        notices = await SpendLedger(book, clock=lambda: now).notices()
        misses = await ModelCallMisses(board).between(now - timedelta(days=1), now)
        said = claude_lines(misses, notices)
        out.append("Last day:" if said else "Last day: every Claude run ran; every cap is under 80%.")
        out.extend(f"  {line}" for line in said)
        opus = MODELS[ModelLevel.OPUS].model_id
        estimate = thinking_estimate(await calls.first_with_counts(opus, THINKING_SAMPLE))
        if estimate is None:
            out.append("Opus thinking: no Opus call with counts yet.")
        else:
            out.append(
                f"Opus thinking (estimate, first {estimate.calls} of {THINKING_SAMPLE} calls):"
                f" {estimate.avg_output_tokens} output tokens a call, about {estimate.avg_visible_tokens}"
                f" of them visible, so about {estimate.avg_thinking_tokens} thinking"
                f" (about USD {usd(estimate.avg_thinking_micro)} a call)."
            )
            if not estimate.enough:
                out.append(f"  Not enough calls to decide yet: the decision waits for {THINKING_SAMPLE}.")
        return out
    finally:
        await db.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    prepare: Prepare = laptop_db.prepare,
    clock: Clock = lambda: datetime.now(UTC),
) -> int:
    """Print the report; 0 when it was read."""
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    url = asyncio.run(prepare())
    if url is None:
        print("claude-spend: the laptop database is not running. Start it: python -m tools.laptop_db up")
        return 1
    for line in asyncio.run(lines(url, clock())):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
