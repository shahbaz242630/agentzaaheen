"""The monthly Claude spend caps, in code (ADR-033, F10 slice 1).

The API credit is USD 200 a month with no card behind it, so spending more
is impossible; the caps make sure no one use can eat the month's credit and
leave the others with nothing. Each use has its own cap, and all uses
together stop at USD 180, leaving USD 20 spare. Changing a cap is the
Chairman's call.

Before a call, :meth:`SpendLedger.allows` checks that the call's worst case
still fits under both its use's cap and the total; after it,
:meth:`SpendLedger.record` adds what it really cost. Months are UTC calendar
months. :meth:`SpendLedger.notices` lists every cap at 80% or more, for the
morning briefing. The amounts live in a :class:`SpendBook`: in production
the :class:`StoreBook`, one database row per call, so a restart never resets a
cap.

Two calls checked at the same moment can both pass before either is recorded,
so a cap can be passed by at most what calls in flight cost; the USD 20 spare
under the credit covers that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from agentzaaheen.core.llm_gateway.model_map import MILLION, usd
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import CallTokens, ModelCalls


class Use(StrEnum):
    """What a Claude call is for. Each has its own monthly cap."""

    HEADS = "heads"  # department heads, Project Leads, the Chief Auditor (Opus)
    CODE_REVIEW = "code-review"  # the Code Reviewer (Sonnet)
    COMPUTER_USE = "computer-use"  # computer use in the sandbox (Sonnet)
    BOARD_CHAT = "board-chat"  # the Board chat and the CEO's tools


CAPS_USD: dict[Use, int] = {Use.HEADS: 60, Use.CODE_REVIEW: 20, Use.COMPUTER_USE: 20, Use.BOARD_CHAT: 80}
TOTAL_STOP_USD = 180
NOTICE_PERCENT = 80
TOTAL = "total"  # the name the all-uses cap goes by in notices and logs


class SpendBook(Protocol):
    """Where the month's amounts are kept (millionths of a USD)."""

    async def spent(self, use: Use, month: str) -> int:
        """What ``use`` has spent in ``month`` (``"2026-10"``)."""
        ...

    async def add(  # noqa: PLR0913  # the call's own facts
        self,
        use: Use,
        model: str,
        micro: int,
        at: datetime,
        tokens: CallTokens | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        """Add one call's cost to ``use`` at the moment ``at`` (with its token counts and run, when known)."""
        ...


class MemoryBook:
    """A book held in memory: for tests, and until the database book exists."""

    def __init__(self) -> None:
        self._amounts: dict[tuple[Use, str], int] = {}

    async def spent(self, use: Use, month: str) -> int:
        """What ``use`` has spent in ``month``."""
        return self._amounts.get((use, month), 0)

    async def add(  # noqa: PLR0913  # the model, counts and run are only kept in the database
        self,
        use: Use,
        model: str,  # noqa: ARG002
        micro: int,
        at: datetime,
        tokens: CallTokens | None = None,  # noqa: ARG002
        *,
        run_id: str | None = None,  # noqa: ARG002
    ) -> None:
        """Add one call's cost."""
        month = month_of(at)
        self._amounts[use, month] = self._amounts.get((use, month), 0) + micro


class StoreBook:
    """The book in the database: one row per call, added up per month (it survives restarts)."""

    def __init__(self, calls: ModelCalls) -> None:
        self._calls = calls

    async def spent(self, use: Use, month: str) -> int:
        """What ``use`` has spent in ``month``."""
        start, end = month_bounds(month)
        return await self._calls.spent(use.value, start=start, end=end)

    async def add(  # noqa: PLR0913  # the call's own facts
        self,
        use: Use,
        model: str,
        micro: int,
        at: datetime,
        tokens: CallTokens | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        """Save one call's cost, and its counts and run when known, as its own row."""
        await self._calls.add(use.value, model, micro, at=at, tokens=tokens, run_id=run_id)


@dataclass(frozen=True)
class SpendCheck:
    """The answer before a call. ``refusal`` is ``None`` when it may go."""

    refusal: str | None
    cap: str  # the use, or "total": the cap the answer is about
    spent_micro: int
    cap_micro: int


@dataclass(frozen=True)
class SpendNotice:
    """One cap at 80% or more this month, for the briefing."""

    cap: str
    spent_micro: int
    cap_micro: int

    @property
    def percent(self) -> int:
        """Whole percent used, rounded down (79.9% is not yet 80%)."""
        return self.spent_micro * 100 // self.cap_micro


def month_of(now: datetime) -> str:
    """The UTC calendar month a moment falls in (``"2026-10"``)."""
    return now.astimezone(UTC).strftime("%Y-%m")


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """The first moment of ``month`` (``"2026-10"``) and of the month after, in UTC."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    year, month_number = divmod(start.year * 12 + start.month, 12)  # the month after, carrying into the year
    return start, start.replace(year=year, month=month_number + 1)


class SpendLedger:
    """Checks each Claude call against its use's cap and the total, and adds up what it cost."""

    def __init__(
        self,
        book: SpendBook,
        *,
        caps_usd: dict[Use, int] | None = None,
        total_usd: int = TOTAL_STOP_USD,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._book = book
        self._caps = {use: usd_cap * MILLION for use, usd_cap in (caps_usd or CAPS_USD).items()}
        self._total = total_usd * MILLION
        self._clock = clock
        self._log = get_logger("agentzaaheen.spend")

    async def allows(self, use: Use, upto_micro: int) -> SpendCheck:
        """Whether a call that may cost up to ``upto_micro`` still fits under both caps this month."""
        month = month_of(self._clock())
        spent = await self._book.spent(use, month)
        if spent + upto_micro > self._caps[use]:
            return SpendCheck("use_cap_reached", use.value, spent, self._caps[use])
        total = await self._total_spent(month)
        if total + upto_micro > self._total:
            return SpendCheck("total_stop_reached", TOTAL, total, self._total)
        return SpendCheck(None, use.value, spent, self._caps[use])

    async def record(  # the call's own facts
        self, use: Use, model: str, micro: int, tokens: CallTokens | None = None, *, run_id: str | None = None
    ) -> None:
        """Add one call's real cost (for ``run_id``, when known) and say so; and if it crossed an 80% line."""
        now = self._clock()
        month = month_of(now)
        before = await self._book.spent(use, month)
        total_before = await self._total_spent(month)
        await self._book.add(use, model, micro, now, tokens, run_id=run_id)
        after = before + micro
        say(
            self._log,
            "model_spend_recorded",
            model=model,
            use=use.value,
            cost_usd=usd(micro),
            spent_usd=usd(after),
            cap_usd=usd(self._caps[use]),
        )
        for cap, old, new, limit in (
            (use.value, before, after, self._caps[use]),
            (TOTAL, total_before, total_before + micro, self._total),
        ):
            if _percent(old, limit) < NOTICE_PERCENT <= _percent(new, limit):
                say(
                    self._log,
                    "model_spend_notice",
                    use=cap,
                    percent=_percent(new, limit),
                    cap_usd=usd(limit),
                )

    async def notices(self) -> list[SpendNotice]:
        """Every cap at 80% or more this month (each use, then the total)."""
        month = month_of(self._clock())
        found = [
            SpendNotice(use.value, spent, cap)
            for use, cap in self._caps.items()
            if _percent(spent := await self._book.spent(use, month), cap) >= NOTICE_PERCENT
        ]
        total = await self._total_spent(month)
        if _percent(total, self._total) >= NOTICE_PERCENT:
            found.append(SpendNotice(TOTAL, total, self._total))
        return found

    async def _total_spent(self, month: str) -> int:
        return sum([await self._book.spent(use, month) for use in self._caps])


def _percent(spent: int, cap: int) -> int:
    return spent * 100 // cap if cap > 0 else 100
