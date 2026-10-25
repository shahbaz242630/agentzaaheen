"""The gate before every model call: the role's level, customer data, then the caps (F10 slice 1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.llm_gateway import (
    CAPS_USD,
    MODELS,
    TOTAL_STOP_USD,
    Clearance,
    FixedNames,
    KnownNames,
    MemoryBook,
    ModelGate,
    PromptPart,
    Refusal,
    SpendLedger,
    StoreNames,
    Usage,
    Use,
    prints_of,
)
from agentzaaheen.core.llm_gateway.route import worst_case_micro
from agentzaaheen.core.llm_gateway.spend import month_bounds, month_of
from agentzaaheen.core.sdk import DataClass, ModelLevel, install_log_sink
from agentzaaheen.core.vault import MasterKey, NamePrinter
from tests.fixtures.name_prints import PRINTER

OCT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
OPS = (PromptPart("Head of Engineering: 3 builds failed this week.", DataClass.INTERNAL),)
SECRET_WORDS = "Head of Engineering: 3 builds failed"


@pytest.fixture
def lines() -> Iterator[list[dict[str, object]]]:
    found: list[dict[str, object]] = []
    install_log_sink(lambda line: found.append(dict(line)))
    yield found
    install_log_sink(None)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


NAMES = FixedNames(KnownNames.of(["Fatima Al Mansoori", "Grace"], PRINTER))


def gate(clock: Clock | None = None, book: MemoryBook | None = None) -> tuple[ModelGate, SpendLedger]:
    ledger = SpendLedger(book or MemoryBook(), clock=clock or Clock(OCT))
    return ModelGate(ledger, NAMES), ledger


def clear(  # mirrors the gate's own knobs
    g: ModelGate,
    *,
    role_level: ModelLevel = ModelLevel.OPUS,
    wanted: ModelLevel = ModelLevel.OPUS,
    use: Use | None = Use.HEADS,
    parts: tuple[PromptPart, ...] = OPS,
    max_output_tokens: int = 3_000,
) -> Clearance:
    return asyncio.run(
        g.clear(
            role="head-of-engineering",
            role_level=role_level,
            wanted=wanted,
            parts=parts,
            max_output_tokens=max_output_tokens,
            use=use,
        )
    )


def events(lines: list[dict[str, object]]) -> list[object]:
    return [line["event"] for line in lines]


# ---- the role's level


def test_a_head_may_call_opus_and_is_told_the_worst_case(lines: list[dict[str, object]]) -> None:
    g, _ = gate()
    result = clear(g)
    assert result.refusal is None
    assert result.model == MODELS[ModelLevel.OPUS]
    assert result.upto_micro == worst_case_micro(MODELS[ModelLevel.OPUS], OPS, 3_000)
    assert events(lines) == ["model_cloud_check_passed", "model_call_cleared"]


@pytest.mark.parametrize(
    ("role_level", "wanted"),
    [
        (ModelLevel.LOCAL, ModelLevel.SONNET),
        (ModelLevel.SONNET, ModelLevel.OPUS),
        (ModelLevel.CODE, ModelLevel.LOCAL),
    ],
)
def test_a_call_above_the_roles_level_is_refused(
    role_level: ModelLevel, wanted: ModelLevel, lines: list[dict[str, object]]
) -> None:
    g, _ = gate()
    assert clear(g, role_level=role_level, wanted=wanted).refusal is Refusal.ABOVE_ROLE
    assert events(lines) == ["model_call_refused"]
    assert lines[0]["model_level"] == int(wanted)
    assert lines[0]["role_level"] == int(role_level)


def test_level_0_has_no_model_even_for_the_strongest_role() -> None:
    g, _ = gate()
    assert clear(g, wanted=ModelLevel.CODE).refusal is Refusal.NO_MODEL


def test_our_own_model_needs_no_use_and_no_customer_data_check(lines: list[dict[str, object]]) -> None:
    g, _ = gate()
    customer = (PromptPart("Fatima asked for a refund.", DataClass.CUSTOMER_PII),)
    result = clear(g, role_level=ModelLevel.LOCAL, wanted=ModelLevel.LOCAL, use=None, parts=customer)
    assert result.refusal is None
    assert result.model == MODELS[ModelLevel.LOCAL]
    assert result.upto_micro == 0
    assert events(lines) == ["model_call_cleared_local"]


def test_a_cloud_call_must_say_what_it_is_for() -> None:
    g, _ = gate()
    assert clear(g, use=None).refusal is Refusal.NO_USE


# ---- customer data


@pytest.mark.parametrize(
    "part",
    [
        PromptPart("Fatima Al Mansoori asked for a refund.", DataClass.CUSTOMER_PII),
        PromptPart("Email fatima.m@example.ae back.", DataClass.INTERNAL),
        PromptPart("Customer ctm_01hv8x4k2m9q7r3t5w6y8z0abc churned.", DataClass.INTERNAL),
    ],
)
def test_customer_data_refuses_a_cloud_call_and_the_log_never_holds_it(
    part: PromptPart, lines: list[dict[str, object]]
) -> None:
    g, ledger = gate()
    assert clear(g, parts=(*OPS, part)).refusal is Refusal.CUSTOMER_DATA
    assert events(lines) == ["model_customer_data_refused"]
    written = json.dumps(lines)
    assert part.text not in written
    assert "Fatima" not in written
    assert "example.ae" not in written
    assert asyncio.run(ledger.notices()) == []


def test_one_of_our_customers_names_refuses_a_cloud_call(lines: list[dict[str, object]]) -> None:
    g, _ = gate()
    named = PromptPart("Fatima Al Mansoori asked about the refund.", DataClass.INTERNAL)
    assert clear(g, parts=(*OPS, named)).refusal is Refusal.CUSTOMER_DATA
    assert lines[-1]["reason"] == "known_customer_name"
    assert "Fatima" not in json.dumps(lines)


def test_each_name_block_says_which_kind_of_name_it_was(lines: list[dict[str, object]]) -> None:
    g, _ = gate()
    assert clear(g, parts=(*OPS, PromptPart("Grace asked about it.", DataClass.INTERNAL))).refusal is (
        Refusal.CUSTOMER_DATA
    )
    assert lines[-1]["reason"] == "known_customer_single_name"
    assert (
        clear(g, parts=(*OPS, PromptPart("A 7-day grace period applies.", DataClass.INTERNAL))).refusal
        is None
    )


def test_a_gate_with_no_name_list_refuses_every_cloud_call_but_not_our_own_models() -> None:
    g = ModelGate(SpendLedger(MemoryBook(), clock=Clock(OCT)))
    assert clear(g).refusal is Refusal.CUSTOMER_DATA
    local = clear(g, role_level=ModelLevel.LOCAL, wanted=ModelLevel.LOCAL, use=None)
    assert local.refusal is None


class BrokenNames:
    async def known_names(self) -> KnownNames:
        raise RuntimeError("database down")


def test_a_name_list_that_cannot_be_read_refuses_the_call_and_says_why(
    lines: list[dict[str, object]],
) -> None:
    g = ModelGate(SpendLedger(MemoryBook(), clock=Clock(OCT)), BrokenNames())
    assert clear(g).refusal is Refusal.CUSTOMER_DATA
    assert events(lines) == ["model_names_unreadable", "model_customer_data_refused"]
    assert lines[-1]["reason"] == "no_name_list"


def test_no_prompt_text_is_ever_logged(lines: list[dict[str, object]]) -> None:
    g, _ = gate()
    clear(g)
    asyncio.run(g.record(Use.HEADS, MODELS[ModelLevel.OPUS], Usage(20_000, 3_000)))
    assert SECRET_WORDS not in json.dumps(lines)


# ---- the caps


def test_the_caps_are_the_agreed_ones() -> None:
    assert CAPS_USD == {Use.HEADS: 60, Use.CODE_REVIEW: 20, Use.COMPUTER_USE: 20, Use.BOARD_CHAT: 80}
    assert TOTAL_STOP_USD == 180


def test_a_call_whose_worst_case_passes_its_use_cap_is_refused(lines: list[dict[str, object]]) -> None:
    book = MemoryBook()
    asyncio.run(book.add(Use.CODE_REVIEW, "claude-opus-5-5", 19_990_000, OCT))
    g, _ = gate(book=book)
    result = clear(g, role_level=ModelLevel.SONNET, wanted=ModelLevel.SONNET, use=Use.CODE_REVIEW)
    assert result.refusal is Refusal.USE_CAP
    refused = lines[-1]
    assert refused["event"] == "model_spend_refused"
    assert (refused["use"], refused["spent_usd"], refused["cap_usd"]) == ("code-review", "19.9900", "20.0000")


def test_a_call_that_still_fits_is_cleared_right_up_to_the_cap() -> None:
    book = MemoryBook()
    upto = worst_case_micro(MODELS[ModelLevel.OPUS], OPS, 3_000)
    asyncio.run(book.add(Use.HEADS, "claude-opus-5-5", 60_000_000 - upto, OCT))
    g, _ = gate(book=book)
    assert clear(g).refusal is None


def test_the_total_stops_everything_even_when_a_use_has_room() -> None:
    book = MemoryBook()
    asyncio.run(book.add(Use.HEADS, "claude-opus-5-5", 10_000_000, OCT))
    g = ModelGate(SpendLedger(book, total_usd=10, clock=Clock(OCT)), NAMES)
    result = clear(g, role_level=ModelLevel.OPUS, wanted=ModelLevel.SONNET, use=Use.BOARD_CHAT)
    assert result.refusal is Refusal.TOTAL_STOP


def test_last_months_spend_does_not_count_this_month() -> None:
    book = MemoryBook()
    asyncio.run(book.add(Use.HEADS, "claude-opus-5-5", 60_000_000, OCT - timedelta(days=31)))
    g, _ = gate(book=book)
    assert clear(g).refusal is None


def test_months_are_utc_calendar_months() -> None:
    dubai_morning_on_the_first = datetime.fromisoformat("2026-11-01T02:00:00+04:00")
    assert month_of(dubai_morning_on_the_first) == "2026-10"


@pytest.mark.parametrize(
    ("month", "start", "end"),
    [
        ("2026-10", "2026-10-01", "2026-11-01"),
        ("2026-12", "2026-12-01", "2027-01-01"),  # December runs into the next year
        ("2027-01", "2027-01-01", "2027-02-01"),
    ],
)
def test_a_months_bounds_are_its_first_moment_and_the_next_months(month: str, start: str, end: str) -> None:
    utc = [datetime.fromisoformat(day).replace(tzinfo=UTC) for day in (start, end)]
    assert list(month_bounds(month)) == utc


def test_the_memory_book_files_a_cost_under_the_utc_month_it_was_spent_in() -> None:
    book, dubai_morning_on_the_first = MemoryBook(), datetime.fromisoformat("2026-11-01T02:00:00+04:00")
    asyncio.run(book.add(Use.HEADS, "claude-opus-5-5", 5, dubai_morning_on_the_first))
    assert asyncio.run(book.spent(Use.HEADS, "2026-10")) == 5
    assert asyncio.run(book.spent(Use.HEADS, "2026-11")) == 0


def test_recording_adds_the_real_cost_and_says_so(lines: list[dict[str, object]]) -> None:
    g, ledger = gate()
    cost = asyncio.run(g.record(Use.HEADS, MODELS[ModelLevel.OPUS], Usage(20_000, 3_000)))
    assert cost == 140_000  # 20k x 4 + 3k x 20 = USD 0.14, the estimate in hr/_shared/models.md
    assert events(lines) == ["model_spend_recorded"]
    assert (lines[0]["cost_usd"], lines[0]["spent_usd"]) == ("0.1400", "0.1400")
    assert asyncio.run(ledger.allows(Use.HEADS, 0)).spent_micro == 140_000


def test_our_own_models_cost_nothing_and_touch_no_cap() -> None:
    g, ledger = gate()
    assert asyncio.run(g.record(Use.HEADS, MODELS[ModelLevel.LOCAL], Usage(50_000, 5_000))) == 0
    assert asyncio.run(ledger.allows(Use.HEADS, 0)).spent_micro == 0


def test_crossing_80_percent_is_said_once_and_listed_for_the_briefing(lines: list[dict[str, object]]) -> None:
    _, ledger = gate()
    opus = MODELS[ModelLevel.OPUS]
    asyncio.run(ledger.record(Use.CODE_REVIEW, opus.model_id, 15_000_000))
    asyncio.run(ledger.record(Use.CODE_REVIEW, opus.model_id, 1_000_000))  # 80%: crosses the line
    asyncio.run(ledger.record(Use.CODE_REVIEW, opus.model_id, 1_000_000))  # 85%: already past it
    notices = [line for line in lines if line["event"] == "model_spend_notice"]
    assert [(n["use"], n["percent"]) for n in notices] == [("code-review", 80)]
    [notice] = asyncio.run(ledger.notices())
    assert (notice.cap, notice.percent) == ("code-review", 85)


def test_the_total_gets_its_own_80_percent_notice() -> None:
    book = MemoryBook()
    for use in (Use.HEADS, Use.CODE_REVIEW, Use.COMPUTER_USE, Use.BOARD_CHAT):
        asyncio.run(book.add(use, "claude-opus-5-5", CAPS_USD[use] * 810_000, OCT))  # 81% of each
    ledger = SpendLedger(book, clock=Clock(OCT))
    assert [n.cap for n in asyncio.run(ledger.notices())] == [
        "heads",
        "code-review",
        "computer-use",
        "board-chat",
        "total",
    ]


def test_just_under_80_percent_is_not_yet_a_notice() -> None:
    book = MemoryBook()
    asyncio.run(book.add(Use.HEADS, "claude-opus-5-5", 47_999_999, OCT))
    assert asyncio.run(SpendLedger(book, clock=Clock(OCT)).notices()) == []


OWN_KEY = NamePrinter(MasterKey("names-7", bytes(range(1, 33))))


class Prints:
    def __init__(self, names: list[str]) -> None:
        self.rows = {(p.kind.value, p.print) for p in prints_of(names, OWN_KEY)}
        self.asked: list[str] = []

    async def made_with(self, key_id: str) -> set[tuple[str, bytes]]:
        self.asked.append(key_id)
        return self.rows


def test_the_stored_list_is_read_fresh_for_each_call_with_this_keys_prints_only() -> None:
    rows = Prints(["Fatima Al Mansoori"])
    g = ModelGate(SpendLedger(MemoryBook(), clock=Clock(OCT)), StoreNames(rows, OWN_KEY))
    named = PromptPart("Fatima Al Mansoori asked again.", DataClass.INTERNAL)
    assert clear(g, parts=(*OPS, named)).refusal is Refusal.CUSTOMER_DATA
    assert clear(g).refusal is None
    rows.rows |= {(p.kind.value, p.print) for p in prints_of(["Grace"], OWN_KEY)}  # a name added since
    assert (
        clear(g, parts=(*OPS, PromptPart("Grace wrote in.", DataClass.INTERNAL))).refusal
        is Refusal.CUSTOMER_DATA
    )
    assert rows.asked == ["names-7"] * 3  # only prints made with this key
