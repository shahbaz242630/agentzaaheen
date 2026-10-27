"""The one gate every model call passes before it is made (ADR-033, F10 slice 1).

Three checks, in this order, each logged in plain English:

1. **The role's level.** A role may use only a model at or below the level its
   pack names. Level 0 has no model at all.
2. **Customer data** (cloud models only): one part tagged customer data, one
   of our own customers' names, or anything the second net finds refuses the
   call; nothing is trimmed. With no name list, no cloud call may go.
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
from typing import Protocol

from agentzaaheen.core.llm_gateway.cloud_check import (
    Found,
    KnownNames,
    NamePrint,
    PrintKind,
    PromptPart,
    customer_data_in,
)
from agentzaaheen.core.llm_gateway.model_map import MODELS, ModelEntry, Usage, cost_micro_usd, usd
from agentzaaheen.core.llm_gateway.spend import SpendLedger, Use
from agentzaaheen.core.sdk import ModelLevel, contains_secret, get_logger, say
from agentzaaheen.core.store import CallTokens


class Refusal(StrEnum):
    """Why the gate said no. Safe to log, store and show."""

    NO_MODEL = "model.no_model"  # level 0: plain code, no model
    ABOVE_ROLE = "model.above_role_level"
    NO_USE = "model.no_use"  # a cloud call must say what it is for, so it has a cap
    CUSTOMER_DATA = "model.customer_data"
    SECRET = "model.secret"  # noqa: S105  # a reason code: a key or password was in the prompt (F11 slice 3)
    USE_CAP = "model.use_cap_reached"
    TOTAL_STOP = "model.total_stop_reached"


@dataclass(frozen=True)
class Clearance:
    """The gate's answer. ``model`` is set only on yes; ``upto_micro`` is the worst case it allowed."""

    refusal: Refusal | None
    model: ModelEntry | None = None
    upto_micro: int = 0


class NameSource(Protocol):
    """Where our own customers' names come from (the store, slice 2b), read fresh for each cloud call."""

    async def known_names(self) -> KnownNames:
        """Every customer name we hold, across all workspaces."""
        ...


class FixedNames:
    """A name list that does not change: for tests, and an explicit empty list before there are customers."""

    def __init__(self, names: KnownNames) -> None:
        self._names = names

    async def known_names(self) -> KnownNames:
        """The same list every time."""
        return self._names


class KeyedPrinter(Protocol):
    """A name printer and the id of its key (the vault's ``NamePrinter``)."""

    key_id: str

    def __call__(self, text: str) -> bytes:
        """The fingerprint of ``text``."""
        ...


class PrintRows(Protocol):
    """Where the fingerprints are kept (the store's ``AllNamePrints``)."""

    async def made_with(self, key_id: str) -> set[tuple[str, bytes]]:
        """Every ``(kind, print)`` made with ``key_id``."""
        ...


class StoreNames:
    """The name list from the database: every workspace's fingerprints made with this printer's key.

    Read fresh for each cloud call, so a name added a moment ago already counts.
    A read that fails raises, and the gate then refuses the call.
    """

    def __init__(self, prints: PrintRows, printer: KeyedPrinter) -> None:
        self._prints = prints
        self._printer = printer

    async def known_names(self) -> KnownNames:
        """Every customer name we hold, as fingerprints, across all workspaces."""
        rows = await self._prints.made_with(self._printer.key_id)
        return KnownNames((NamePrint(PrintKind(kind), value) for kind, value in rows), self._printer)


class ModelGate:
    """Decides whether one role may make one model call, and records what Claude calls cost."""

    def __init__(self, ledger: SpendLedger, names: NameSource | None = None) -> None:
        self._ledger = ledger
        self._names = names
        self._log = get_logger("agentzaaheen.models")

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
        if any(contains_secret(part.text) for part in parts):  # before anything leaves (F11 slice 3)
            say(self._log, "model_secret_refused", role=role, model=entry.model_id)
            return Clearance(Refusal.SECRET)
        found = customer_data_in(parts, await self._known_names())
        if found is not None:
            say(self._log, "model_customer_data_refused", role=role, model=entry.model_id, reason=found.value)
            return Clearance(Refusal.CUSTOMER_DATA)
        say(self._log, "model_cloud_check_passed", model=entry.model_id, checked=len(parts))
        return await self._spend(role, entry, use, parts, max_output_tokens)

    async def record(  # the call's own facts
        self, use: Use, model: ModelEntry, usage: Usage, answer_chars: int = 0, *, run_id: str | None = None
    ) -> int:
        """Add what one call really cost (from the API's counts) and keep the counts; returns micro-USD.

        ``run_id`` is the agent run the call was for, so the runner can add up the run's cost (F11).

        ``answer_chars`` is the visible answer's length: with ``usage.output_tokens`` it shows how
        much of the output was hidden thinking (Opus thinks on every call; F10 slice 3b).
        """
        micro = cost_micro_usd(model.price, usage)
        if model.cloud:
            tokens = CallTokens(
                tokens_in=usage.input_tokens,
                tokens_out=usage.output_tokens,
                cache_read=usage.cache_read_tokens,
                cache_write=usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
                answer_chars=answer_chars,
            )
            await self._ledger.record(use, model.model_id, micro, tokens, run_id=run_id)
        return micro

    async def _known_names(self) -> KnownNames | None:
        """The name list, or ``None`` (so the call is refused) if there is none or it could not be read."""
        if self._names is None:
            return None
        try:
            return await self._names.known_names()
        except Exception:  # noqa: BLE001  # whatever failed, no list means no cloud call
            say(self._log, "model_names_unreadable", reason=Found.NO_NAME_LIST.value)
            return None

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
