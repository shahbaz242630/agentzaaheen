"""One live call to Claude through Agent Zaaheen's own door, to prove the key, the gate and the costing work.

Sends one short fixed question (no customer data: our own words) through the
same path every agent will use: the model gate, the customer-data check, the
one Claude client in ``core/net``, then the cost from the pinned prices. Prints
the outcome, the short answer and what it cost; never the key.

The answer is capped at ``MAX_OUTPUT_TOKENS``, so one check costs well under
one US cent. Its cost is added to this month's spend in the laptop database,
like every other Claude call, so the caps count it.

The key is opened from the vault for this one call (F10 slice 2c): it is saved
once on the dashboard's Customize page, sealed after Windows Hello. It is never
read from a file or an environment variable. The laptop database must be
running (``tools.laptop_db up``) and the vault's master key file in place
(``tools.vault_key make-key``).

Usage:
    uv run python -m tools.claude_check [--model sonnet|opus]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import (
    MODELS,
    Answer,
    CloudAsk,
    FixedNames,
    KnownNames,
    MissBook,
    ModelGate,
    PromptPart,
    SpendBook,
    SpendLedger,
    StoreBook,
    StoreMisses,
    Use,
)
from agentzaaheen.core.llm_gateway.model_map import usd
from agentzaaheen.core.net import ClaudeClient
from agentzaaheen.core.sdk import DataClass, ModelLevel, explain
from agentzaaheen.core.store import Database, DatabaseSettings, ModelCallMisses, ModelCalls
from agentzaaheen.core.vault import Keyring
from tools import laptop_db, vault_key

MAX_OUTPUT_TOKENS = 64
LEVELS = {"sonnet": ModelLevel.SONNET, "opus": ModelLevel.OPUS}
SYSTEM = PromptPart(
    "You are checking that a connection works. Answer in one short sentence.", DataClass.PUBLIC
)
TASK = PromptPart("Say that the connection to Claude works.", DataClass.PUBLIC)
NO_KEY_SAVED = "credential.missing"

KeySource = Callable[[], Awaitable[SecretStr]]
Prepare = Callable[[], Coroutine[Any, Any, SecretStr | None]]


async def check(
    key: KeySource,
    level: ModelLevel,
    book: SpendBook,
    misses: MissBook,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Answer:
    """One call through the gate and the door, its cost written to ``book`` (or why not, to ``misses``)."""
    pinned = frozenset(entry.model_id for entry in MODELS.values() if entry.cloud)
    gate = ModelGate(SpendLedger(book), FixedNames(KnownNames.empty()))  # our own words: no names to hold
    async with ClaudeClient(key, models=pinned, transport=transport) as door:
        return await CloudAsk(gate, door, misses).ask(
            role="claude-check",
            role_level=level,
            wanted=level,
            use=Use.CODE_REVIEW if level is ModelLevel.SONNET else Use.HEADS,
            system=(SYSTEM,),
            task=(TASK,),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            run_id="claude-check",
        )


async def check_saved_key(
    url: SecretStr, keyring: Keyring, level: ModelLevel, transport: httpx.AsyncBaseTransport | None
) -> Answer:
    """The check with the key sealed in the laptop database, its cost added to the month's spend."""
    db = await Database.open(DatabaseSettings(dsn=url, min_size=1, max_size=2))
    try:
        slot = vault_key.claude_slot(db, keyring)
        board = db.scoped(laptop_db.LAPTOP_TENANT)
        return await check(
            slot.open, level, StoreBook(ModelCalls(board)), StoreMisses(ModelCallMisses(board)), transport
        )
    finally:
        await db.close()


def report(answer: Answer, model: str) -> tuple[int, list[str]]:
    """The exit code and the lines to print."""
    if answer.outcome == "answered":
        return 0, [
            f"claude-check: {model} answered.",
            f"  Answer: {answer.text.strip()[:200]}",
            f"  Cost: USD {usd(answer.cost_micro)} (added to this month's spend)",
        ]
    if answer.reason == NO_KEY_SAVED:
        return 1, [
            f"claude-check: {model} was not called: no Claude key is saved.",
            "  Save it once on the dashboard's Customize page (Windows Hello confirms it), then try again.",
        ]
    if answer.reason:
        said = explain(answer.reason, vendor="claude")
        status = f", http {answer.status}" if answer.status is not None else ""
        lines = [f"claude-check: {model} failed with {answer.reason}{status}."]
        if answer.status == 402:  # Anthropic's "credit used up"
            lines.append("  Most likely: the API credit for this month is used up, or billing is not set up.")
        return 1, [*lines, f"  What: {said.what}", f"  Why: {said.why}", f"  What to do: {said.fix}"]
    return 1, [f"claude-check: {model} did not answer: {answer.outcome}."]


def main(
    argv: Sequence[str] | None = None,
    *,
    key_file: Path = vault_key.KEY_FILE,
    prepare: Prepare = laptop_db.prepare,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run the check; 0 when Claude answered, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=sorted(LEVELS), default="sonnet")
    ns = parser.parse_args(argv)
    keyring = vault_key.load_keyring(key_file)
    if keyring is None:
        print("claude-check: could not run, not a pass.")
        return 1
    url = asyncio.run(prepare())
    if url is None:
        print("claude-check: the laptop database is not running. Start it: python -m tools.laptop_db up")
        return 1
    level = LEVELS[ns.model]
    answer = asyncio.run(check_saved_key(url, keyring, level, transport))
    code, lines = report(answer, MODELS[level].model_id)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
