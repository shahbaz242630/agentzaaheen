"""Put real cards over a portfolio's sample ones, one card at a time.

Each real card is built on every request, so its freshness is judged at the
moment it is shown: data read an hour ago turns to "unknown" by itself once it
is older than its rule allows. A project counts as sample while any of its
cards still is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from agentzaaheen.core.api.app import Portfolio
from agentzaaheen.core.api.views import Card, ModelList, ProjectDetail, ProjectSummary

CardMaker = Callable[[], Card]


class OverlayPortfolio:
    """``base`` with some cards replaced, keyed by ``(project id, card id)``."""

    def __init__(self, base: Portfolio, cards: Mapping[tuple[str, str], CardMaker]) -> None:
        for project_id, card_id in cards:
            detail = base.project(project_id)
            if detail is None or all(card.id != card_id for card in detail.cards):
                raise KeyError(f"no card {card_id!r} in project {project_id!r} to replace")
        self._base = base
        self._cards = dict(cards)

    def projects(self) -> tuple[ProjectSummary, ...]:
        """The base list, with each project's sample flag following its cards."""
        out: list[ProjectSummary] = []
        for summary in self._base.projects():
            detail = self.project(summary.id)
            out.append(detail.project if detail is not None else summary)
        return tuple(out)

    def project(self, project_id: str) -> ProjectDetail | None:
        """The base project with any real cards swapped in."""
        detail = self._base.project(project_id)
        if detail is None:
            return None
        cards = tuple(self._card(project_id, card) for card in detail.cards)
        sample = any(card.sample for card in cards)
        return ProjectDetail(project=detail.project.model_copy(update={"sample": sample}), cards=cards)

    def models(self) -> ModelList:
        """The base model list, unchanged."""
        return self._base.models()

    def _card(self, project_id: str, card: Card) -> Card:
        make = self._cards.get((project_id, card.id))
        if make is None:
            return card
        real = make()
        if real.id != card.id:
            raise ValueError(f"card maker for {card.id!r} returned {real.id!r}")
        return real
