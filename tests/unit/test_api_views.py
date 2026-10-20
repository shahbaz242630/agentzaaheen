from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from yaqzan.core.api import SamplePortfolio
from yaqzan.core.api.views import (
    Card,
    Count,
    Metric,
    ModelChoice,
    ModelList,
    Money,
    NotKnown,
    Percent,
    ProjectDetail,
    ProjectSummary,
)
from yaqzan.core.metrics import Freshness

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _card(freshness: Freshness, value: Count | NotKnown, **rest: object) -> Card:
    return Card.model_validate(
        {
            "id": "paddle",
            "title": "Revenue",
            "source": "Paddle",
            "freshness": freshness,
            "sample": True,
            "metrics": (Metric(label="MRR", value=value),),
            **rest,
        }
    )


def test_a_fresh_card_may_show_numbers() -> None:
    card = _card(Freshness.FRESH, Count(value=3), last_success_at=NOW)
    assert card.metrics[0].value == Count(value=3)


@pytest.mark.parametrize("state", [Freshness.STALE, Freshness.ALERT, Freshness.NEVER])
def test_a_card_that_is_not_fresh_cannot_show_a_number(state: Freshness) -> None:
    with pytest.raises(ValidationError, match="must be unknown"):
        _card(state, Count(value=3))


@pytest.mark.parametrize("state", [Freshness.STALE, Freshness.ALERT])
def test_a_stale_card_shows_unknown(state: Freshness) -> None:
    card = _card(state, NotKnown(reason="freshness.stale"), last_success_at=NOW)
    assert isinstance(card.metrics[0].value, NotKnown)


def test_a_card_that_never_synced_has_no_last_success_time() -> None:
    with pytest.raises(ValidationError, match="never synced"):
        _card(Freshness.NEVER, NotKnown(reason="source.not_connected"), last_success_at=NOW)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "money", "minor": 100, "currency": "usd"},
        {"kind": "money", "minor": 1.5, "currency": "USD"},
        {"kind": "count", "value": -1},
        {"kind": "percent", "basis_points": 10_001},
        {"kind": "unknown", "reason": "Not Known!"},
        {"kind": "float", "value": 1.0},
    ],
)
def test_values_are_strict(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Metric.model_validate({"label": "x", "value": bad})


def test_values_travel_without_floats() -> None:
    metric = Metric(label="Uptime", value=Percent(basis_points=9_995))
    assert metric.model_dump(mode="json")["value"] == {"kind": "percent", "basis_points": 9_995}
    money = Metric(label="MRR", value=Money(minor=124_000, currency="USD"))
    assert money.model_dump(mode="json")["value"] == {"kind": "money", "minor": 124_000, "currency": "USD"}


def test_views_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Count.model_validate({"kind": "count", "value": 1, "email": "a@b.c"})


def test_card_ids_are_unique_within_a_project() -> None:
    summary = ProjectSummary(id="p", name="P", summary="", health="ok", escalations=0, sample=True)
    card = _card(Freshness.FRESH, Count(value=1), last_success_at=NOW)
    with pytest.raises(ValidationError, match="unique"):
        ProjectDetail(project=summary, cards=(card, card))


def _model(model_id: str, where: str, *, default: bool) -> ModelChoice:
    return ModelChoice.model_validate(
        {
            "id": model_id,
            "label": model_id,
            "where": where,
            "available": False,
            "default": default,
            "note": "",
        }
    )


@pytest.mark.parametrize(
    "models",
    [
        (),
        (_model("a", "local", default=False),),
        (_model("a", "local", default=True), _model("b", "local", default=True)),
        (_model("a", "cloud", default=True), _model("b", "local", default=False)),
    ],
)
def test_the_chat_has_exactly_one_default_and_it_is_local(models: tuple[ModelChoice, ...]) -> None:
    with pytest.raises(ValidationError, match="ADR-023"):
        ModelList(models=models)


def test_the_sample_is_marked_sample_everywhere() -> None:
    portfolio = SamplePortfolio(NOW)
    for summary in portfolio.projects():
        assert summary.sample
        detail = portfolio.project(summary.id)
        assert detail is not None
        assert all(card.sample for card in detail.cards)


def test_the_sample_lists_the_five_products_with_zaaheen_first() -> None:
    names = [p.name for p in SamplePortfolio(NOW).projects()]
    assert names == ["Zaaheen", "Sandoq Kin", "Agent X", "Eva", "Family Check-in"]


def test_the_sample_support_card_shows_counts_not_addresses() -> None:
    detail = SamplePortfolio(NOW).project("zaaheen")
    assert detail is not None
    support = next(card for card in detail.cards if card.id == "support")
    assert [m.label for m in support.metrics] == ["Responded", "Escalated to you", "Waiting"]
    assert "@" not in support.model_dump_json()


def test_the_sample_times_follow_now() -> None:
    detail = SamplePortfolio(NOW).project("zaaheen")
    assert detail is not None
    times = [card.last_success_at for card in detail.cards if card.last_success_at is not None]
    assert times
    assert all(t < NOW for t in times)


def test_the_sample_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SamplePortfolio(datetime(2026, 10, 18))  # noqa: DTZ001


def test_an_unknown_project_is_none() -> None:
    assert SamplePortfolio(NOW).project("nope") is None
