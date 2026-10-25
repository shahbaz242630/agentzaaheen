"""One live call to Claude through Yaqzan's own door, to prove the key, the gate and the costing work.

Sends one short fixed question (no customer data: our own words) through the
same path every agent will use: the model gate, the customer-data check, the
one Claude client in ``core/net``, then the cost from the pinned prices. Prints
the outcome, the short answer and what it cost; never the key.

The answer is capped at ``MAX_OUTPUT_TOKENS``, so one check costs well under
one US cent. Its cost is shown but kept in memory only, not added to the
monthly caps in the database.

The key is read from the environment variable you name (never printed). Not
``ANTHROPIC_API_KEY``: Claude Code reads that one too, and would then bill the
API credit for its own work.

Usage:
    uv run python -m tools.claude_check --key-env YAQZAN_CLAUDE_KEY [--model sonnet|opus]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence

import httpx
from pydantic import SecretStr

from yaqzan.core.llm_gateway import (
    MODELS,
    Answer,
    CloudAsk,
    FixedNames,
    KnownNames,
    MemoryBook,
    ModelGate,
    PromptPart,
    SpendLedger,
    Use,
)
from yaqzan.core.llm_gateway.model_map import usd
from yaqzan.core.net import ClaudeClient
from yaqzan.core.sdk import DataClass, ModelLevel, explain

MAX_OUTPUT_TOKENS = 64
LEVELS = {"sonnet": ModelLevel.SONNET, "opus": ModelLevel.OPUS}
SYSTEM = PromptPart(
    "You are checking that a connection works. Answer in one short sentence.", DataClass.PUBLIC
)
TASK = PromptPart("Say that the connection to Claude works.", DataClass.PUBLIC)
RESERVED_ENV = "ANTHROPIC_API_KEY"


async def check(
    key: SecretStr, level: ModelLevel, transport: httpx.AsyncBaseTransport | None = None
) -> Answer:
    """One call through the gate and the door."""

    async def key_source() -> SecretStr:
        return key

    pinned = frozenset(entry.model_id for entry in MODELS.values() if entry.cloud)
    gate = ModelGate(SpendLedger(MemoryBook()), FixedNames(KnownNames([])))  # our own words: no names to hold
    async with ClaudeClient(key_source, models=pinned, transport=transport) as door:
        return await CloudAsk(gate, door).ask(
            role="claude-check",
            role_level=level,
            wanted=level,
            use=Use.CODE_REVIEW if level is ModelLevel.SONNET else Use.HEADS,
            system=(SYSTEM,),
            task=(TASK,),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            run_id="claude-check",
        )


def report(answer: Answer, model: str) -> tuple[int, list[str]]:
    """The exit code and the lines to print."""
    if answer.outcome == "answered":
        return 0, [
            f"claude-check: {model} answered.",
            f"  Answer: {answer.text.strip()[:200]}",
            f"  Cost: USD {usd(answer.cost_micro)} (not added to the monthly caps)",
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
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run the check; 0 when Claude answered, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-env", required=True, help="name of the env var holding the Claude API key")
    parser.add_argument("--model", choices=sorted(LEVELS), default="sonnet")
    ns = parser.parse_args(argv)
    if ns.key_env == RESERVED_ENV:
        print(f"claude-check: do not use {RESERVED_ENV}; Claude Code would bill the API for its own work.")
        print("  Put the key in YAQZAN_CLAUDE_KEY instead.")
        return 1
    raw = (os.environ if environ is None else environ).get(ns.key_env, "").strip()
    if not raw:
        print(f"claude-check: {ns.key_env} is not set (could not run, not a pass).")
        return 1
    if not raw.isascii() or not raw.isprintable() or " " in raw:
        print("claude-check: the key contains spaces or control characters; copy it again.")
        return 1
    if not raw.startswith("sk-ant-"):
        print("claude-check: that does not look like a Claude API key (they start with sk-ant-).")
        return 1
    level = LEVELS[ns.model]
    answer = asyncio.run(check(SecretStr(raw), level, transport))
    code, lines = report(answer, MODELS[level].model_id)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
