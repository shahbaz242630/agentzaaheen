"""Checkpoint 3: every model answer must have the role's shape, or it is sent back (ADR-036, F11 slice 2).

An agent never holds the model itself. The runner gives it a
:class:`CheckedModel`, whose only way to ask is :meth:`CheckedModel.answer`
with a :class:`Shape`: code that turns the answer into the role's checked
result, or refuses it with a :class:`ShapeRefusal` (a code for the logs and
a plain sentence for the model). A refused answer is sent back with that
sentence, at most :data:`MAX_RETRIES` more times; then the run ends as
**needs a look** (:class:`NeedsALookError`), never with a blank or a guess.

Each try is one model call and one of the run's steps. What the model wrote
is never logged; only codes and counts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from agentzaaheen.core.llm_gateway import ChatModel
from agentzaaheen.core.sdk import get_logger, say

MAX_RETRIES = 2  # after the first answer: three tries in all
NEEDS_A_LOOK = "run.needs_a_look"
_CODE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_RETRY = (
    "Your answer was refused by the checks: {tell} "
    "Answer again, following the rules and the exact shape you were given. Nothing else."
)


class ShapeRefusal(Exception):  # noqa: N818  # a refusal, not an error: the run goes on
    """An answer that does not have the role's shape: ``code`` for the logs, ``tell`` for the model."""

    def __init__(self, code: str, tell: str) -> None:
        super().__init__(code)
        self.code = code if _CODE.fullmatch(code) and len(code) <= 64 else "shape.refused"
        self.tell = tell


class AgentFailedError(Exception):
    """A run that ended for a known reason: its ``code`` is saved on the run (``could_not_run``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NeedsALookError(AgentFailedError):
    """Every try was refused: the run needs a person to look. ``last`` is the last refusal's code."""

    def __init__(self, last: str) -> None:
        super().__init__(NEEDS_A_LOOK)
        self.last = last


class Shape[T](Protocol):
    """A role's answer shape: the checked result, or a :class:`ShapeRefusal`."""

    def check(self, answer: str) -> T:
        """The answer, checked by code."""
        ...


class CheckedModel:
    """The model as an agent sees it: it can only ask for an answer in a shape."""

    def __init__(self, model: ChatModel, *, role: str, run_id: str) -> None:
        self._model = model
        self._role = role
        self._run_id = run_id
        self._log = get_logger("agentzaaheen.runner")
        self.attempts = 0
        self.last_refusal: str | None = None

    async def answer[T](
        self, messages: Sequence[Mapping[str, str]], shape: Shape[T], *, max_tokens: int, temperature: float
    ) -> T:
        """The model's answer in ``shape``; refused answers go back with the reason, at most twice."""
        talk = [dict(m) for m in messages]
        for attempt in range(1, MAX_RETRIES + 2):
            self.attempts += 1
            text = await self._model.chat(talk, max_tokens=max_tokens, temperature=temperature)
            try:
                return shape.check(text)
            except ShapeRefusal as refusal:
                self.last_refusal = refusal.code
                say(self._log, "agent_answer_refused", role=self._role, run_id=self._run_id,
                    attempt=attempt, code=refusal.code)  # fmt: skip
                talk = [*talk, {"role": "assistant", "content": text},
                        {"role": "user", "content": _RETRY.format(tell=refusal.tell)}]  # fmt: skip
        say(self._log, "agent_needs_a_look", role=self._role, run_id=self._run_id, attempt=self.attempts,
            code=self.last_refusal or "shape.refused")  # fmt: skip
        raise NeedsALookError(self.last_refusal or "shape.refused")
