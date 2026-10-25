"""The Board's Claude lines for the morning briefing, from fixed templates (ADR-033, F10 slice 3a).

Two things, each said only when there is something to say:

- the runs that **did not run** since the last briefing, each with its role and
  why, in our own words (at most :data:`MAX_NAMED`, then "and N more"), so a run
  that did not happen is never mistaken for one that did;
- every spend cap at 80% or more this month (:meth:`SpendLedger.notices`).

:func:`thinking_estimate` (slice 3b) turns the first Opus calls' saved counts
into an estimate of how much of each answer was hidden thinking, for the
decision ADR-033 leaves to evidence ("measure the first 20 runs").

Only codes and our own words reach the phone: role ids, use names, and the
fixed phrases below. An unknown code is shown as itself, never dropped.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from agentzaaheen.core.llm_gateway.ask import DECLINED, UNREACHABLE
from agentzaaheen.core.llm_gateway.model_map import MILLION, MODELS
from agentzaaheen.core.llm_gateway.route import Refusal
from agentzaaheen.core.llm_gateway.spend import TOTAL, SpendNotice
from agentzaaheen.core.sdk import ModelLevel
from agentzaaheen.core.store import CallRow, MissRow

MAX_NAMED = 3
THINKING_SAMPLE = 20  # ADR-033: set Opus thinking from the first 20 runs
CHARS_PER_TOKEN = 4  # a rough English average; the estimate says so

WHY: dict[str, str] = {
    Refusal.USE_CAP.value: "its monthly cap is reached",
    Refusal.TOTAL_STOP.value: "the USD 180 monthly stop is reached",
    Refusal.CUSTOMER_DATA.value: "its prompt held customer data, so it was not sent",
    Refusal.ABOVE_ROLE.value: "it asked for a model above its role",
    Refusal.NO_USE.value: "it did not say what the call was for",
    Refusal.NO_MODEL.value: "its role uses no model",
    UNREACHABLE: "Claude could not be reached",
    DECLINED: "Claude declined to answer",
}
# Why Claude could not be reached, when the network said: added after the words above.
WHY_UNREACHABLE: dict[str, str] = {
    "net.auth": "the key was refused",
    "net.rate_limited": "Claude was busy",
    "net.timeout": "it timed out",
    "credential.missing": "no Claude key is saved",
}
CAP_NAMES: dict[str, str] = {
    "heads": "department heads",
    "code-review": "code review",
    "computer-use": "computer use",
    "board-chat": "Board chat",
    TOTAL: "all Claude use",
}


def _why(miss: MissRow) -> str:
    why = WHY.get(miss.outcome, miss.outcome)
    if miss.outcome == UNREACHABLE and miss.detail is not None:
        why += f" ({WHY_UNREACHABLE.get(miss.detail, miss.detail)})"
    return why


def claude_lines(misses: Sequence[MissRow], notices: Sequence[SpendNotice]) -> tuple[str, ...]:
    """The briefing's Claude lines; none when every run ran and every cap is under 80%."""
    lines: list[str] = []
    if misses:
        count = len(misses)
        named = "; ".join(f"{m.role}: {_why(m)}" for m in misses[:MAX_NAMED])
        more = f"; and {count - MAX_NAMED} more" if count > MAX_NAMED else ""
        runs = "1 Claude run" if count == 1 else f"{count} Claude runs"
        lines.append(f"{runs} did not run ({named}{more}). Nothing answered in their place.")
    for notice in notices:
        name = CAP_NAMES.get(notice.cap, notice.cap)
        cap = notice.cap_micro // MILLION  # caps are whole dollars
        lines.append(f"Claude spend: {name} at {notice.percent}% of its USD {cap} cap this month.")
    return tuple(lines)


@dataclass(frozen=True)
class ThinkingEstimate:
    """How much of the first Opus calls' output was hidden thinking: an estimate, never a billed figure."""

    calls: int
    avg_output_tokens: int
    avg_visible_tokens: int  # the visible answer's characters / CHARS_PER_TOKEN
    avg_thinking_tokens: int
    avg_thinking_micro: int  # what that thinking cost per call, at Opus's output price

    @property
    def enough(self) -> bool:
        """Whether the sample is big enough to decide on (ADR-033: 20 calls)."""
        return self.calls >= THINKING_SAMPLE


def thinking_estimate(calls: Sequence[CallRow]) -> ThinkingEstimate | None:
    """The estimate from saved Opus calls with counts; ``None`` when there are none yet."""
    if not calls:
        return None
    count = len(calls)
    out = sum(c.tokens.tokens_out for c in calls) // count
    visible = sum(c.tokens.answer_chars for c in calls) // CHARS_PER_TOKEN // count
    thinking = max(0, out - visible)
    price = MODELS[ModelLevel.OPUS].price
    if price is None:  # pragma: no cover  # Opus always has a price; a missing one is a map bug
        raise ValueError("Opus has no price in the model map")
    # USD per million tokens x tokens = millionths of a USD; rounded up, like every cost here
    per_call = math.ceil(thinking * price.output)
    return ThinkingEstimate(count, out, visible, thinking, per_call)
