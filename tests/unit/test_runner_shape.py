"""Checkpoint 3: an answer in the wrong shape goes back with the reason, at most twice (F11 slice 2)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from agentzaaheen.core.runner import (
    MAX_RETRIES,
    NEEDS_A_LOOK,
    AgentFailedError,
    CheckedModel,
    NeedsALookError,
    ShapeRefusal,
)


class Model:
    """Answers in turn from ``replies`` (the last one again when they run out); keeps what it was sent."""

    def __init__(self, *replies: str) -> None:
        self.replies = replies
        self.sent: list[list[dict[str, str]]] = []

    async def chat(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float
    ) -> str:
        self.sent.append([dict(m) for m in messages])
        return self.replies[min(len(self.sent), len(self.replies)) - 1]


class Number:
    """A shape: a whole number, or refused."""

    def check(self, answer: str) -> int:
        if not answer.strip().isdigit():
            raise ShapeRefusal("test.not_a_number", "it must be a whole number and nothing else.")
        return int(answer)


ASK = [{"role": "system", "content": "Count."}, {"role": "user", "content": "How many?"}]


def answer(model: Model, checked: CheckedModel | None = None) -> int:
    door = checked or CheckedModel(model, role="tester", run_id="run-1")
    return asyncio.run(door.answer(ASK, Number(), max_tokens=10, temperature=0.0))


def test_a_good_first_answer_is_one_try() -> None:
    model = Model("7")
    door = CheckedModel(model, role="tester", run_id="run-1")
    assert answer(model, door) == 7
    assert door.attempts == 1
    assert model.sent == [ASK]


def test_a_refused_answer_goes_back_with_its_reason_and_the_next_is_kept() -> None:
    model = Model("seven", "7")
    door = CheckedModel(model, role="tester", run_id="run-1")
    assert answer(model, door) == 7
    assert door.attempts == 2
    again = model.sent[1]
    assert again[:2] == ASK  # the same rules and task, never shortened
    assert again[2] == {"role": "assistant", "content": "seven"}
    assert again[3]["role"] == "user"
    assert "it must be a whole number and nothing else." in again[3]["content"]


def test_after_two_more_refusals_it_needs_a_look_and_nothing_is_guessed() -> None:
    model = Model("seven")
    door = CheckedModel(model, role="tester", run_id="run-1")
    with pytest.raises(NeedsALookError) as caught:
        answer(model, door)
    assert len(model.sent) == door.attempts == 1 + MAX_RETRIES == 3
    assert (caught.value.code, caught.value.last) == (NEEDS_A_LOOK, "test.not_a_number")
    assert isinstance(caught.value, AgentFailedError)  # the runner saves its code on the run


def test_a_refusal_code_that_is_not_a_code_is_never_stored_as_one() -> None:
    assert ShapeRefusal("Not A Code!", "x").code == "shape.refused"
    assert ShapeRefusal("a" * 70, "x").code == "shape.refused"
    assert ShapeRefusal("scout.not_json", "x").code == "scout.not_json"
