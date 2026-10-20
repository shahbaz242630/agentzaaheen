"""What the dashboard is sent: projects, their cards, and the chat's model list.

These shapes are the contract between the API and the web UI (the UI checks
every response against the same shapes with zod). Money travels as integer
minor units with its currency, times as UTC; the UI formats both.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yaqzan.core.metrics import Freshness

_SLUG = r"^[a-z0-9][a-z0-9-]{0,39}$"
_CODE = r"^[a-z0-9_]+(\.[a-z0-9_]+)*$"


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Money(_View):
    """An amount in integer minor units (cents, pence) of one currency."""

    kind: Literal["money"] = "money"
    minor: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Count(_View):
    """How many of something."""

    kind: Literal["count"] = "count"
    value: int = Field(ge=0)


class Percent(_View):
    """A percentage in basis points (99.95% is 9995), so no floats travel."""

    kind: Literal["percent"] = "percent"
    basis_points: int = Field(ge=0, le=10_000)


class NotKnown(_View):
    """A number we cannot give truthfully; shown as "unknown", never as 0."""

    kind: Literal["unknown"] = "unknown"
    reason: str = Field(pattern=_CODE)


Value = Annotated[Money | Count | Percent | NotKnown, Field(discriminator="kind")]


class Metric(_View):
    """One labelled number on a card."""

    label: str = Field(min_length=1, max_length=40)
    value: Value
    detail: str | None = Field(default=None, max_length=80)


class Tone(StrEnum):
    """How an entry should look: fine, neutral, needs a look, or urgent."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ALERT = "alert"


class Entry(_View):
    """One line in a card's list, e.g. an escalation or something an agent did."""

    text: str = Field(min_length=1, max_length=160)
    tone: Tone = Tone.INFO
    at: datetime | None = None


class Card(_View):
    """One source's view of a project: numbers plus a short list of lines.

    A card whose data is not fresh may only carry unknown numbers (PRD F1.7),
    so a stale card can never show an old figure as if it were current.
    """

    id: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=40)
    source: str = Field(min_length=1, max_length=40)
    freshness: Freshness
    last_success_at: datetime | None = None
    sample: bool
    metrics: tuple[Metric, ...] = ()
    entries: tuple[Entry, ...] = ()

    @model_validator(mode="after")
    def _stale_means_unknown(self) -> Card:
        if not self.freshness.usable and any(not isinstance(m.value, NotKnown) for m in self.metrics):
            raise ValueError(f"card {self.id!r} is {self.freshness}: its numbers must be unknown")
        if self.freshness is Freshness.NEVER and self.last_success_at is not None:
            raise ValueError(f"card {self.id!r} never synced but has a last success time")
        return self


class Health(StrEnum):
    """A project's overall colour on the list."""

    OK = "ok"
    WARN = "warn"
    ALERT = "alert"
    UNKNOWN = "unknown"


class ProjectSummary(_View):
    """A project as listed on the left of the dashboard."""

    id: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1, max_length=40)
    summary: str = Field(max_length=80)
    health: Health
    escalations: int = Field(ge=0)
    sample: bool


class ProjectDetail(_View):
    """Everything the centre of the dashboard shows for one project."""

    project: ProjectSummary
    cards: tuple[Card, ...]

    @model_validator(mode="after")
    def _unique_cards(self) -> ProjectDetail:
        ids = [card.id for card in self.cards]
        if len(ids) != len(set(ids)):
            raise ValueError("card ids must be unique within a project")
        return self


class ModelChoice(_View):
    """One model the chat can switch to (ADR-023: local by default, cloud opt-in)."""

    id: str = Field(pattern=_SLUG)
    label: str = Field(min_length=1, max_length=40)
    where: Literal["local", "cloud"]
    available: bool
    default: bool
    note: str = Field(max_length=120)


class ModelList(_View):
    """The chat's model menu. Exactly one default, and it is local."""

    models: tuple[ModelChoice, ...]

    @model_validator(mode="after")
    def _one_local_default(self) -> ModelList:
        defaults = [m for m in self.models if m.default]
        if len(defaults) != 1 or defaults[0].where != "local":
            raise ValueError("there must be exactly one default model, and it must be local (ADR-023)")
        return self


class ChatQuestion(_View):
    """One question typed into the chat, about one project or (``None``) all of them."""

    project_id: str | None = Field(default=None, pattern=_SLUG)
    model_id: str = Field(pattern=_SLUG)
    question: str = Field(min_length=1, max_length=2000)


class ChatAnswer(_View):
    """The model's plain-text answer. ``from_sample`` says some facts it saw were sample data."""

    model_id: str = Field(pattern=_SLUG)
    answer: str = Field(min_length=1, max_length=4000)
    from_sample: bool
