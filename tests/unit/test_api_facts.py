"""The cards as plain text for the chat model: same numbers, unknowns and sample tags as the screen."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentzaaheen.core.api import SamplePortfolio
from agentzaaheen.core.api.facts import money_text, portfolio_facts, project_facts, uses_sample
from agentzaaheen.core.api.views import (
    Card,
    Count,
    Entry,
    Metric,
    Money,
    NotKnown,
    Percent,
    ProjectDetail,
    Tone,
)
from agentzaaheen.core.metrics import Freshness

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [
        (124_000, "USD", "USD 1240.00"),
        (5_850, "GBP", "GBP 58.50"),
        (-1_900, "EUR", "EUR -19.00"),
        (500, "JPY", "JPY 500"),
        (1_234, "KWD", "KWD 1.234"),
        (7, "USD", "USD 0.07"),
    ],
)
def test_money_uses_the_currency_decimals(minor: int, currency: str, text: str) -> None:
    assert money_text(Money(minor=minor, currency=currency)) == text


def _detail(*cards: Card) -> ProjectDetail:
    base = SamplePortfolio(NOW).project("zaaheen")
    assert base is not None
    project = base.project.model_copy(update={"sample": False})
    return ProjectDetail(project=project, cards=cards)


def test_a_card_is_written_out_line_by_line() -> None:
    card = Card(
        id="paddle",
        title="Revenue",
        source="Paddle",
        freshness=Freshness.FRESH,
        last_success_at=NOW,
        sample=False,
        metrics=(
            Metric(label="MRR", value=Money(minor=5_850, currency="USD")),
            Metric(label="Trials", value=Count(value=3), detail="this month"),
            Metric(label="Uptime", value=Percent(basis_points=9995)),
            Metric(label="Refunds", value=NotKnown(reason="mrr.price_not_exact")),
        ),
        entries=(Entry(text="A payment failed", tone=Tone.WARN, at=NOW),),
    )
    facts = project_facts(_detail(card))
    assert facts.splitlines()[1:] == [
        "Card: Revenue (source Paddle; real; data fresh; last read 2026-10-18T12:00:00+00:00)",
        "  - MRR: USD 58.50",
        "  - Trials: 3; this month",
        "  - Uptime: 99.95%",
        "  - Refunds: unknown (mrr.price_not_exact)",
        "  * (warn) [2026-10-18T12:00:00+00:00] A payment failed",
    ]
    assert not uses_sample(_detail(card))


def test_sample_cards_are_tagged() -> None:
    detail = SamplePortfolio(NOW).project("zaaheen")
    assert detail is not None
    facts = project_facts(detail)
    assert "SAMPLE (made-up numbers)" in facts
    assert facts.startswith(
        "Project: Zaaheen (Memory Vault desktop app); health warn; 2 escalated for review"
    )
    assert uses_sample(detail)


def test_a_never_synced_card_says_so() -> None:
    card = Card(
        id="stripe",
        title="Stripe",
        source="Stripe",
        freshness=Freshness.NEVER,
        sample=False,
        metrics=(Metric(label="MRR", value=NotKnown(reason="source.not_connected")),),
    )
    facts = project_facts(_detail(card))
    assert "data never; last read never" in facts
    assert "MRR: unknown (source.not_connected)" in facts


def test_the_project_list() -> None:
    portfolio = SamplePortfolio(NOW)
    lines = portfolio_facts(portfolio.projects()).splitlines()
    assert len(lines) == 5
    assert all(line.startswith("Project: ") for line in lines)
    assert portfolio_facts(()) == "No projects."
