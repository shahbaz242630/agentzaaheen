"""Asking Claude: clear the call, send it through the one door, record what it cost (ADR-033, F10 slice 2a).

:class:`CloudAsk` is the only way a role reaches Claude. It asks the
:class:`~agentzaaheen.core.llm_gateway.route.ModelGate` first; only a cleared call
is sent. The role pack and fixed rules go first as the system prefix (so
they can be cached), the task last. Every call is logged with its role,
model, use, run ID, token counts and cost, never its text.

What comes back is an :class:`Answer` whose ``outcome`` is ``"answered"`` or
a code saying why not. A call that was refused, could not reach Claude, or
that Claude declined is **never** answered by a weaker model instead: the
caller marks its run "did not run" and names it in the briefing.

Every ask that ends without an answer is written to a :class:`MissBook` (the
database's ``model_call_misses`` in real use), codes only, so the morning
briefing can name it (F10 slice 3a). If that write fails, the line
``model_miss_not_recorded`` says so and the caller still gets its answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agentzaaheen.core.llm_gateway.cloud_check import PromptPart
from agentzaaheen.core.llm_gateway.model_map import MODELS, Usage, usd
from agentzaaheen.core.llm_gateway.route import ModelGate
from agentzaaheen.core.llm_gateway.spend import Use
from agentzaaheen.core.net import ClaudeReply
from agentzaaheen.core.sdk import ModelLevel, SourceError, get_logger, say
from agentzaaheen.core.store import MissRow, ModelCallMisses, StoreError

ANSWERED = "answered"
UNREACHABLE = "model.unreachable"
DECLINED = "model.declined"  # Claude's own safety check stopped the answer (stop reason "refusal")


class ClaudeDoor(Protocol):
    """The one door to Claude (``core.net.ClaudeClient`` in real use)."""

    async def message(  # the request's own parts
        self,
        *,
        model: str,
        system: Sequence[str],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        effort: str | None = None,
    ) -> ClaudeReply:
        """Claude's reply to one request."""
        ...


@dataclass(frozen=True)
class Answer:
    """What happened to one ask: ``outcome`` is ``"answered"`` or why not; ``cost_micro`` is what it cost.

    When Claude could not be reached, ``reason`` is the network's code (``net.auth``,
    ``net.rate_limited``, ...) and ``status`` the HTTP status, if there was one.
    """

    outcome: str
    text: str = ""
    stop_reason: str = ""
    cost_micro: int = 0
    reason: str = ""
    status: int | None = None


class MissBook(Protocol):
    """Where runs that did not run are written (``StoreMisses`` in real use)."""

    async def add(self, miss: MissRow) -> None:
        """Keep one miss."""
        ...


class MemoryMisses:
    """Misses kept in memory (tests, and the dashboard without a database)."""

    def __init__(self) -> None:
        self.misses: list[MissRow] = []

    async def add(self, miss: MissRow) -> None:
        """Keep one miss."""
        self.misses.append(miss)


class StoreMisses:
    """Misses kept in the database (``model_call_misses``): the briefing reads them."""

    def __init__(self, misses: ModelCallMisses) -> None:
        self._misses = misses

    async def add(self, miss: MissRow) -> None:
        """Keep one miss."""
        await self._misses.add(miss)


class CloudAsk:
    """Clears, sends and records one Claude call at a time; a run that did not run is written down."""

    def __init__(
        self,
        gate: ModelGate,
        door: ClaudeDoor,
        misses: MissBook,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate
        self._door = door
        self._misses = misses
        self._clock = clock
        self._log = get_logger("agentzaaheen.models")

    async def ask(  # noqa: PLR0913  # each is a separate fact about the call
        self,
        *,
        role: str,
        role_level: ModelLevel,
        wanted: ModelLevel,
        use: Use,
        system: Sequence[PromptPart],
        task: Sequence[PromptPart],
        max_output_tokens: int,
        run_id: str,
        effort: str | None = None,
    ) -> Answer:
        """Ask the ``wanted`` level's Claude model, if the gate allows it; write it down if it did not run."""
        if wanted < ModelLevel.SONNET:
            raise ValueError("CloudAsk is for Claude levels only; our own models are asked directly")
        answer = await self._ask(
            role=role,
            role_level=role_level,
            wanted=wanted,
            use=use,
            system=system,
            task=task,
            max_output_tokens=max_output_tokens,
            run_id=run_id,
            effort=effort,
        )
        if answer.outcome != ANSWERED:
            await self._missed(answer, role=role, use=use, model=MODELS[wanted].model_id, run_id=run_id)
        return answer

    async def _missed(self, answer: Answer, *, role: str, use: Use, model: str, run_id: str) -> None:
        miss = MissRow(
            at=self._clock(),
            role=role,
            use=use.value,
            model=model,
            outcome=answer.outcome,
            detail=answer.reason or None,
            run_id=run_id,
        )
        try:
            await self._misses.add(miss)
        except StoreError as exc:  # the caller still gets its answer; the line says the briefing will miss it
            say(self._log, "model_miss_not_recorded", role=role, run_id=run_id, code=exc.code.value)

    async def _ask(  # noqa: PLR0913  # each is a separate fact about the call
        self,
        *,
        role: str,
        role_level: ModelLevel,
        wanted: ModelLevel,
        use: Use,
        system: Sequence[PromptPart],
        task: Sequence[PromptPart],
        max_output_tokens: int,
        run_id: str,
        effort: str | None,
    ) -> Answer:
        cleared = await self._gate.clear(
            role=role,
            role_level=role_level,
            wanted=wanted,
            parts=(*system, *task),
            max_output_tokens=max_output_tokens,
            use=use,
        )
        if cleared.refusal is not None or cleared.model is None:
            return Answer(str(cleared.refusal))
        model = cleared.model
        try:
            reply = await self._door.message(
                model=model.model_id,
                system=[part.text for part in system],
                messages=[{"role": "user", "content": "\n\n".join(part.text for part in task)}],
                max_tokens=max_output_tokens,
                effort=effort,
            )
        except SourceError as exc:
            say(
                self._log,
                "model_cloud_call_failed",
                role=role,
                model=model.model_id,
                use=use.value,
                run_id=run_id,
                code=exc.reason_code,
            )
            return Answer(UNREACHABLE, reason=exc.reason_code, status=exc.status)
        usage = Usage(
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cache_read_tokens=reply.cache_read_tokens,
            cache_write_5m_tokens=reply.cache_write_5m_tokens,
            cache_write_1h_tokens=reply.cache_write_1h_tokens,
        )
        cost = await self._gate.record(use, model, usage, len(reply.text))
        say(
            self._log,
            "model_cloud_call_done",
            role=role,
            model=model.model_id,
            use=use.value,
            run_id=run_id,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
            cache_write=usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
            cost_usd=usd(cost),
            stop=reply.stop_reason,
        )
        if reply.stop_reason == "refusal":
            return Answer(DECLINED, stop_reason=reply.stop_reason, cost_micro=cost)
        return Answer(ANSWERED, reply.text, reply.stop_reason, cost)
