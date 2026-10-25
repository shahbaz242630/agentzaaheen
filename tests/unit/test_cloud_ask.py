"""Asking Claude: clear, send through the one door, record the cost; never a weaker model (F10 slice 2a).

A fake door stands in for ``core.net.ClaudeClient``: no network, no key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime

import pytest

from yaqzan.core.llm_gateway import (
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
from yaqzan.core.net import ClaudeReply, NetError, NetErrorCode
from yaqzan.core.sdk import DataClass, ModelLevel, install_log_sink

OCT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
PACK = PromptPart("You are the Head of Engineering. You only read and draft.", DataClass.INTERNAL)
TASK = PromptPart("This week: 3 builds failed on Zaaheen, 1 on Eva.", DataClass.INTERNAL)
REPLY = ClaudeReply(
    text="Fix Zaaheen's build first.",
    stop_reason="end_turn",
    input_tokens=20_000,
    output_tokens=3_000,
    cache_read_tokens=0,
    cache_write_5m_tokens=0,
    cache_write_1h_tokens=0,
)


@pytest.fixture
def lines() -> Iterator[list[dict[str, object]]]:
    found: list[dict[str, object]] = []
    install_log_sink(lambda line: found.append(dict(line)))
    yield found
    install_log_sink(None)


class FakeDoor:
    def __init__(self, reply: ClaudeReply | Exception = REPLY) -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def message(  # mirrors the door
        self,
        *,
        model: str,
        system: Sequence[str],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        effort: str | None = None,
    ) -> ClaudeReply:
        self.calls.append(
            {"model": model, "system": list(system), "messages": list(messages), "max_tokens": max_tokens}
        )
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def setup(door: FakeDoor) -> tuple[CloudAsk, SpendLedger]:
    ledger = SpendLedger(MemoryBook(), clock=lambda: OCT)
    return CloudAsk(ModelGate(ledger, FixedNames(KnownNames(["Fatima Al Mansoori"]))), door), ledger


def ask(
    asker: CloudAsk,
    *,
    task: tuple[PromptPart, ...] = (TASK,),
    role_level: ModelLevel = ModelLevel.OPUS,
) -> Answer:
    return asyncio.run(
        asker.ask(
            role="head-of-engineering",
            role_level=role_level,
            wanted=ModelLevel.OPUS,
            use=Use.HEADS,
            system=(PACK,),
            task=task,
            max_output_tokens=3_000,
            run_id="run-42",
        )
    )


def test_a_cleared_call_is_sent_with_the_pack_first_and_its_cost_recorded(
    lines: list[dict[str, object]],
) -> None:
    door = FakeDoor()
    asker, ledger = setup(door)
    answer = ask(asker)
    assert answer == Answer("answered", "Fix Zaaheen's build first.", "end_turn", 140_000)
    [call] = door.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == [PACK.text]
    assert call["messages"] == [{"role": "user", "content": TASK.text}]
    assert call["max_tokens"] == 3_000
    assert asyncio.run(ledger.allows(Use.HEADS, 0)).spent_micro == 140_000
    done = lines[-1]
    assert done["event"] == "model_cloud_call_done"
    assert (done["run_id"], done["tokens_in"], done["tokens_out"], done["cost_usd"]) == (
        "run-42",
        20_000,
        3_000,
        "0.1400",
    )
    written = json.dumps(lines)
    assert TASK.text not in written
    assert PACK.text not in written
    assert REPLY.text not in written


def test_a_refused_call_never_reaches_the_door() -> None:
    door = FakeDoor()
    asker, _ = setup(door)
    named = PromptPart("Fatima Al Mansoori is unhappy.", DataClass.INTERNAL)
    assert ask(asker, task=(TASK, named)).outcome == "model.customer_data"
    assert ask(asker, role_level=ModelLevel.SONNET).outcome == "model.above_role_level"
    assert door.calls == []


def test_a_pack_holding_customer_data_is_refused_too() -> None:
    door = FakeDoor()
    asker, _ = setup(door)
    result = asyncio.run(
        asker.ask(
            role="head-of-customers",
            role_level=ModelLevel.OPUS,
            wanted=ModelLevel.OPUS,
            use=Use.HEADS,
            system=(PromptPart("Remember fatima.m@example.ae", DataClass.INTERNAL),),
            task=(TASK,),
            max_output_tokens=3_000,
            run_id="run-43",
        )
    )
    assert result.outcome == "model.customer_data"
    assert door.calls == []


def test_an_unreachable_claude_is_did_not_run_with_no_cost_and_no_weaker_answer(
    lines: list[dict[str, object]],
) -> None:
    door = FakeDoor(NetError(NetErrorCode.RATE_LIMITED, "busy", status=529))
    asker, ledger = setup(door)
    assert ask(asker) == Answer("model.unreachable", reason="net.rate_limited", status=529)
    assert len(door.calls) == 1  # asked once, never retried, never handed to another model
    assert asyncio.run(ledger.allows(Use.HEADS, 0)).spent_micro == 0
    assert lines[-1]["event"] == "model_cloud_call_failed"
    assert lines[-1]["code"] == "net.rate_limited"


def test_a_declined_answer_is_not_an_answer_but_its_cost_still_counts() -> None:
    declined = ClaudeReply("", "refusal", 5_000, 10, 0, 0, 0)
    asker, ledger = setup(FakeDoor(declined))
    answer = ask(asker)
    assert (answer.outcome, answer.text) == ("model.declined", "")
    assert answer.cost_micro == 5_000 * 4 + 10 * 20
    assert asyncio.run(ledger.allows(Use.HEADS, 0)).spent_micro == answer.cost_micro


def test_cloud_ask_is_only_for_claude_levels() -> None:
    asker, _ = setup(FakeDoor())
    with pytest.raises(ValueError, match="Claude levels only"):
        asyncio.run(
            asker.ask(
                role="marketing",
                role_level=ModelLevel.LOCAL,
                wanted=ModelLevel.LOCAL,
                use=Use.BOARD_CHAT,
                system=(),
                task=(TASK,),
                max_output_tokens=100,
                run_id="run-44",
            )
        )
