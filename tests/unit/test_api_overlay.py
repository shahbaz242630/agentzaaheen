from datetime import UTC, datetime, timedelta

import pytest

from yaqzan.core.api import SamplePortfolio
from yaqzan.core.api.overlay import OverlayPortfolio
from yaqzan.core.api.views import Card, Count, Metric, NotKnown
from yaqzan.core.metrics import Freshness

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _real(card_id: str = "paddle", *, fresh: bool = True) -> Card:
    if fresh:
        return Card(
            id=card_id,
            title="Revenue",
            source="Paddle",
            freshness=Freshness.FRESH,
            last_success_at=NOW - timedelta(minutes=1),
            sample=False,
            metrics=(Metric(label="Active subscribers", value=Count(value=5)),),
        )
    return Card(
        id=card_id,
        title="Revenue",
        source="Paddle",
        freshness=Freshness.STALE,
        last_success_at=NOW - timedelta(days=1),
        sample=False,
        metrics=(Metric(label="Active subscribers", value=NotKnown(reason="freshness.stale")),),
    )


def test_swaps_in_the_real_card_and_keeps_the_rest() -> None:
    portfolio = OverlayPortfolio(SamplePortfolio(NOW), {("zaaheen", "paddle"): _real})
    detail = portfolio.project("zaaheen")
    assert detail is not None
    paddle = next(card for card in detail.cards if card.id == "paddle")
    assert paddle.sample is False
    assert paddle.metrics[0].value == Count(value=5)
    assert [c.id for c in detail.cards] == [c.id for c in SamplePortfolio(NOW).project("zaaheen").cards]  # type: ignore[union-attr]
    assert detail.project.sample is True  # other cards are still sample


def test_a_project_with_only_real_cards_is_not_sample() -> None:
    makers = {("eva", "paddle"): _real, ("eva", "uptime"): lambda: _real("uptime")}
    portfolio = OverlayPortfolio(SamplePortfolio(NOW), makers)
    eva = next(p for p in portfolio.projects() if p.id == "eva")
    assert eva.sample is False
    assert next(p for p in portfolio.projects() if p.id == "zaaheen").sample is True


def test_builds_the_card_on_every_request() -> None:
    calls: list[bool] = []

    def make() -> Card:
        calls.append(True)
        return _real(fresh=len(calls) == 1)

    portfolio = OverlayPortfolio(SamplePortfolio(NOW), {("zaaheen", "paddle"): make})
    first = portfolio.project("zaaheen")
    later = portfolio.project("zaaheen")
    assert first is not None
    assert later is not None
    assert next(c for c in first.cards if c.id == "paddle").freshness is Freshness.FRESH
    assert next(c for c in later.cards if c.id == "paddle").freshness is Freshness.STALE


@pytest.mark.parametrize("key", [("nope", "paddle"), ("zaaheen", "nope")])
def test_refuses_to_replace_a_card_that_does_not_exist(key: tuple[str, str]) -> None:
    with pytest.raises(KeyError, match="to replace"):
        OverlayPortfolio(SamplePortfolio(NOW), {key: _real})


def test_refuses_a_maker_that_returns_another_card() -> None:
    portfolio = OverlayPortfolio(SamplePortfolio(NOW), {("zaaheen", "paddle"): lambda: _real("stripe")})
    with pytest.raises(ValueError, match="returned 'stripe'"):
        portfolio.project("zaaheen")


def test_passes_through_unknown_projects_and_models() -> None:
    portfolio = OverlayPortfolio(SamplePortfolio(NOW), {})
    assert portfolio.project("nope") is None
    assert portfolio.models() == SamplePortfolio(NOW).models()
    assert portfolio.projects() == SamplePortfolio(NOW).projects()
