"""The model map: which model each level runs on, with pinned ids and prices (ADR-033, F10 slice 1).

Level 0 is plain code and has no model. Levels 1-2 run on our own machines
and cost nothing per call. Levels 3-4 are Claude, paid from the monthly API
credit, so each has a price table, and a call's cost is worked out from the
API's own token counts (thinking tokens are billed as output, and the API
counts them in ``output_tokens``).

Prices are USD per million tokens, from Anthropic's published table (checked
25 Oct 2026). Cache writes cost 1.25x input for the 5-minute cache and 2x for
the 1-hour one; cache reads have their own price. A change of model or price
is an edit here plus a note in ADR-033.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from agentzaaheen.core.sdk import ModelLevel

MILLION = 1_000_000


@dataclass(frozen=True)
class Price:
    """USD per million tokens, for each kind of token the API counts."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal

    @classmethod
    def of(cls, input_usd: str, output_usd: str, cache_read_usd: str) -> Price:
        """A price from the published input, output and cache-read rates; writes follow the multipliers."""
        base = Decimal(input_usd)
        return cls(base, Decimal(output_usd), Decimal(cache_read_usd), base * Decimal("1.25"), base * 2)


@dataclass(frozen=True)
class ModelEntry:
    """One level's model. ``price`` is ``None`` for our own models: they cost nothing per call."""

    level: ModelLevel
    model_id: str
    cloud: bool
    price: Price | None = None


MODELS: dict[ModelLevel, ModelEntry] = {
    ModelLevel.DECIDER: ModelEntry(ModelLevel.DECIDER, "laya", cloud=False),
    ModelLevel.LOCAL: ModelEntry(ModelLevel.LOCAL, "local-gemma", cloud=False),
    ModelLevel.SONNET: ModelEntry(
        ModelLevel.SONNET, "claude-sonnet-5-5", cloud=True, price=Price.of("2", "10", "0.20")
    ),
    ModelLevel.OPUS: ModelEntry(
        ModelLevel.OPUS, "claude-opus-5-5", cloud=True, price=Price.of("4", "20", "0.20")
    ),
}


@dataclass(frozen=True)
class Usage:
    """The token counts one call used, as the API reports them. Never negative."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_5m_tokens,
            self.cache_write_1h_tokens,
        )
        if any(count < 0 for count in counts):
            raise ValueError("token counts are never negative")


def cost_micro_usd(price: Price | None, usage: Usage) -> int:
    """What a call cost in millionths of a USD, rounded up so spend is never under-counted."""
    if price is None:
        return 0
    usd = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_5m_tokens * price.cache_write_5m
        + usage.cache_write_1h_tokens * price.cache_write_1h
    )  # tokens x USD per million tokens = millionths of a USD
    return int(usd.to_integral_value(rounding=ROUND_CEILING))


def usd(micro: int) -> str:
    """Millionths of a USD as a short amount for people ("0.0412")."""
    return f"{Decimal(micro) / MILLION:.4f}"
