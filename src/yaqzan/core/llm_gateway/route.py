"""The one gate every model call passes before it is made (ADR-033, F10 slice 1).

Three checks, in this order, each logged in plain English:

1. **The role's level.** A role may use only a model at or below the level its
   pack names. Level 0 has no model at all.
2. **Customer data** (cloud models only): one part tagged customer data, or
   anything the second net finds, refuses the call; nothing is trimmed.
3. **The spend caps** (cloud models only): the call's worst case must fit
   under its use's cap and the total this month.

The answer is a :class:`Clearance` naming the model to call, or a refusal
code. A refused call is never sent to a weaker model instead: the caller
marks its run "did not run" (ADR-033's fallback rule).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from yaqzan.core.llm_gateway.cloud_check import PromptPart, customer_data_in
from yaqzan.core.llm_gateway.model_map import MODELS, ModelEntry, Usage, cost_micro_usd, usd
from yaqzan.core.llm_gateway.spend import SpendLedger, Use
from yaqzan.core.sdk import ModelLevel, get_logger, say


class Refusal(StrEnum):
    """Why the gate said no. Safe to log, store and show."""

    NO_MODEL = "model.no_model"  # level 0: plain code, no model
    ABOVE_ROLE = "model.above_role_level"
    NO_USE = "model.no_use"  # a cloud call must say what it is for, so it has a cap
    CUSTOMER_DATA = "model.customer_data"
    USE_CAP = "model.use_cap_reached"
    TOTAL_STOP = "model.total_stop_reached"


@dataclass(frozen=True)
class Clearance:
    """The gate's answer. ``model`` is set only on yes; ``upto_micro`` is the worst case it allowed."""

    refusal: Refusal | None
    model: ModelEntry | None = None
    upto_micro: int = 0


class ModelGate:
    """Decides whether one role may make one model call, and records what Claude calls cost."""

    def __init__(self, ledger: SpendLedger) -> None:
        self._ledger = ledger
        self._log = get_logger("yaqzan.models")

    async def clear(  # noqa: PLR0913  # each is a separate fact the gate checks
        self,
        *,
        role: str,
        role_level: ModelLevel,
        wanted: ModelLevel,
        parts: Sequence[PromptPart],
        max_output_tokens: int,
        use: Use | None = None,
    ) -> Clearance:
        """Whether ``role`` may call the ``wanted`` level's model with these prompt parts."""
        entry = MODELS.get(wanted)
        if entry is None or wanted > role_level:
            refusal = Refusal.NO_MODEL if entry is None else Refusal.ABOVE_ROLE
            say(
                self._log,
                "model_call_refused",
                role=role,
                model_level=int(wanted),
                role_level=int(role_level),
                reason=refusal.value,
            )
            return Clearance(refusal)
        if not entry.cloud:
            say(
                self._log,
                "model_call_cleared_local",
                role=role,
                model=entry.model_id,
                model_level=int(wanted),
            )
            return Clearance(None, entry)
        if use is None:
            say(
                self._log,
                "model_call_refused",
                role=role,
                model_level=int(wanted),
                role_level=int(role_level),
                reason=Refusal.NO_USE.value,
            )
            return Clearance(Refusal.NO_USE)
        found = customer_data_in(parts)
        if found is not None:
            say(self._log, "model_customer_data_refused", role=role, model=entry.model_id, reason=found.value)
            return Clearance(Refusal.CUSTOMER_DATA)
        say(self._log, "model_cloud_check_passed", model=entry.model_id, checked=len(parts))
        return await self._spend(role, entry, use, parts, max_output_tokens)

    async def record(self, use: Use, model: ModelEntry, usage: Usage) -> int:
        """Add what one call really cost (from the API's token counts); returns millionths of a USD."""
        micro = cost_micro_usd(model.price, usage)
        if model.cloud:
            await self._ledger.record(use, model.model_id, micro)
        return micro

    async def _spend(  # the facts the spend check needs
        self, role: str, entry: ModelEntry, use: Use, parts: Sequence[PromptPart], max_output_tokens: int
    ) -> Clearance:
        upto = worst_case_micro(entry, parts, max_output_tokens)
        check = await self._ledger.allows(use, upto)
        if check.refusal is not None:
            refusal = Refusal.USE_CAP if check.refusal == "use_cap_reached" else Refusal.TOTAL_STOP
            say(
                self._log,
                "model_spend_refused",
                role=role,
                model=entry.model_id,
                use=check.cap,
                reason=refusal.value,
                estimate_usd=usd(upto),
                spent_usd=usd(check.spent_micro),
                cap_usd=usd(check.cap_micro),
            )
            return Clearance(refusal)
        say(
            self._log,
            "model_call_cleared",
            role=role,
            model=entry.model_id,
            use=use.value,
            estimate_usd=usd(upto),
            spent_usd=usd(check.spent_micro),
            cap_usd=usd(check.cap_micro),
        )
        return Clearance(None, entry, upto)


def worst_case_micro(entry: ModelEntry, parts: Sequence[PromptPart], max_output_tokens: int) -> int:
    """The most a call can cost: at most one token per character in, and every allowed token out."""
    chars = sum(len(part.text) for part in parts)
    return cost_micro_usd(entry.price, Usage(input_tokens=chars, output_tokens=max(0, max_output_tokens)))
