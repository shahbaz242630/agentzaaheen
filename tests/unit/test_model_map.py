"""The model map: levels, pinned model ids and the price table (F10 slice 1, ADR-033)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from yaqzan.core.llm_gateway import MODELS, Usage
from yaqzan.core.llm_gateway.model_map import Price, cost_micro_usd, usd
from yaqzan.core.registry.roles import level_of
from yaqzan.core.sdk import ModelLevel


def test_each_level_runs_on_its_pinned_model() -> None:
    assert {level: entry.model_id for level, entry in MODELS.items()} == {
        ModelLevel.DECIDER: "laya",
        ModelLevel.LOCAL: "local-gemma",
        ModelLevel.SONNET: "claude-sonnet-5-5",
        ModelLevel.OPUS: "claude-opus-5-5",
    }
    assert ModelLevel.CODE not in MODELS  # plain code has no model
    assert all(entry.level is level for level, entry in MODELS.items())


def test_only_claude_is_cloud_and_only_cloud_has_a_price() -> None:
    for entry in MODELS.values():
        assert entry.cloud is (entry.level >= ModelLevel.SONNET)
        assert (entry.price is not None) is entry.cloud


def test_the_prices_are_the_published_ones() -> None:
    opus, sonnet = MODELS[ModelLevel.OPUS].price, MODELS[ModelLevel.SONNET].price
    assert opus == Price(Decimal(4), Decimal(20), Decimal("0.20"), Decimal("5.00"), Decimal(8))
    assert sonnet == Price(Decimal(2), Decimal(10), Decimal("0.20"), Decimal("2.50"), Decimal(4))


def test_every_kind_of_token_is_charged_at_its_own_rate() -> None:
    opus = MODELS[ModelLevel.OPUS].price
    usage = Usage(
        input_tokens=1_000,
        output_tokens=1_000,
        cache_read_tokens=10_000,
        cache_write_5m_tokens=1_000,
        cache_write_1h_tokens=1_000,
    )
    # 1k x 4 + 1k x 20 + 10k x 0.20 + 1k x 5 + 1k x 8 = 39,000 millionths of a USD
    assert cost_micro_usd(opus, usage) == 39_000


def test_a_part_of_a_millionth_is_rounded_up_so_spend_is_never_under_counted() -> None:
    assert cost_micro_usd(MODELS[ModelLevel.OPUS].price, Usage(0, 0, cache_read_tokens=1)) == 1


def test_our_own_models_cost_nothing() -> None:
    assert cost_micro_usd(None, Usage(1_000_000, 1_000_000)) == 0


def test_a_negative_token_count_is_refused() -> None:
    with pytest.raises(ValueError, match="never negative"):
        Usage(input_tokens=-1, output_tokens=0)


def test_amounts_read_as_dollars() -> None:
    assert usd(140_000) == "0.1400"
    assert usd(180_000_000) == "180.0000"


@pytest.mark.parametrize(
    ("model_row", "level"),
    [
        ("Claude Opus 5.5, medium effort (`../../_shared/models.md`)", ModelLevel.OPUS),
        ("Claude Sonnet 5.5 (the exception in `../../_shared/models.md`)", ModelLevel.SONNET),
        ("Local open-source model", ModelLevel.LOCAL),
        ("**Local model only.** It reads customer emails, so never a cloud model", ModelLevel.LOCAL),
        ("Code first; local model explains odd results", ModelLevel.LOCAL),
        ("Code, no AI (Tier 0)", ModelLevel.CODE),
        ("To decide in ADR-033: the local model is weak at code; Claude costs credit", ModelLevel.CODE),
        ("", ModelLevel.CODE),
    ],
)
def test_a_job_cards_model_row_gives_its_level(model_row: str, level: ModelLevel) -> None:
    card = f"# Role\n\n| | |\n|---|---|\n| **Model** | {model_row} |\n| **Highest permission** | Read |\n"
    assert level_of(card) is level


def test_a_card_with_no_model_row_gets_no_model() -> None:
    assert level_of("# Role\n\n| **Highest permission** | Read |\n") is ModelLevel.CODE
