"""What the Customize page is sent: the registry, plus where each connector is in use (F06).

The registry says what exists; ``uses`` says, for each connector, which
projects it serves and whether it was read this run. A skill lists any
connector it needs that is not in the registry, so the page can say so. The
UI checks these shapes with the same rules in zod. Nothing here switches
anything on: "Add" arrives later as a proposal the Chairman approves.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from yaqzan.core.api.views import ProjectDetail
from yaqzan.core.metrics import Freshness
from yaqzan.core.registry import Invocation, Kind, Layout, Registry
from yaqzan.core.sdk import ToolLevel

_SLUG = r"^[a-z0-9][a-z0-9-]{0,39}$"
_ITEM = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_HASH = r"^[0-9a-f]{64}$"


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Use(StrEnum):
    """How a connector stands for one project this run."""

    LIVE = "live"  # read successfully
    FAILED = "failed"  # tried, and the read failed (the card says why)
    NEEDS_KEY = "needs_key"  # not read: no key was given


class ProjectUse(_View):
    """One project a connector serves."""

    project_id: str = Field(pattern=_SLUG)
    status: Use


class ConnectorView(_View):
    """A connector card: what it reads, how far it may go, its key slots (never values), where it is used."""

    id: str = Field(pattern=_ITEM)
    name: str
    category: str
    description: str
    version: str
    level: ToolLevel
    hosts: tuple[str, ...]
    endpoints: tuple[str, ...]
    key_slots: tuple[str, ...]
    fingerprint: str = Field(pattern=_HASH)
    uses: tuple[ProjectUse, ...]


class SkillView(_View):
    """A skill card. ``missing`` are needed connectors the registry does not have."""

    id: str = Field(pattern=_ITEM)
    description: str
    invocation: Invocation
    requires: tuple[str, ...]
    missing: tuple[str, ...]
    fingerprint: str = Field(pattern=_HASH)


class PluginView(_View):
    """A plugin bundle card: what it would bring (nothing in it is run)."""

    id: str = Field(pattern=_ITEM)
    description: str
    version: str | None
    layout: Layout
    skills: int = Field(ge=0)
    connectors: int = Field(ge=0)
    commands: int = Field(ge=0)
    agents: int = Field(ge=0)
    hooks: bool
    fingerprint: str = Field(pattern=_HASH)


class ProblemView(_View):
    """An item that could not be read, and why."""

    kind: Kind
    path: str
    message: str


class RegistryPage(_View):
    """Everything the Customize page shows."""

    connectors: tuple[ConnectorView, ...] = ()
    skills: tuple[SkillView, ...] = ()
    plugins: tuple[PluginView, ...] = ()
    problems: tuple[ProblemView, ...] = ()


def registry_page(registry: Registry, uses: Mapping[str, Mapping[str, Use]]) -> RegistryPage:
    """The page for ``registry``; ``uses`` maps connector id → project id → how it stands."""
    known = {connector.id for connector in registry.connectors}
    return RegistryPage(
        connectors=tuple(
            ConnectorView(
                **c.model_dump(),
                uses=tuple(ProjectUse(project_id=p, status=s) for p, s in uses.get(c.id, {}).items()),
            )
            for c in registry.connectors
        ),
        skills=tuple(
            SkillView(**s.model_dump(), missing=tuple(r for r in s.requires if r not in known))
            for s in registry.skills
        ),
        plugins=tuple(PluginView(**p.model_dump()) for p in registry.plugins),
        problems=tuple(ProblemView(kind=p.kind, path=p.path, message=p.message) for p in registry.problems),
    )


def uses_from_cards(
    project: Callable[[str], ProjectDetail | None],
    wiring: Mapping[str, tuple[str, Sequence[str]]],
) -> dict[str, dict[str, Use]]:
    """How each connector stands per project, judged from the card it feeds (what the screen shows).

    ``wiring`` maps connector id → (card id, project ids). A sample card means
    no key was given; a real card that never synced means the read failed.
    A project or card that does not exist is left out.
    """
    out: dict[str, dict[str, Use]] = {}
    for connector, (card_id, projects) in wiring.items():
        for project_id in projects:
            detail = project(project_id)
            card = next((c for c in detail.cards if c.id == card_id), None) if detail else None
            if card is None:
                continue
            if card.sample:
                status = Use.NEEDS_KEY
            elif card.freshness is Freshness.NEVER:
                status = Use.FAILED
            else:
                status = Use.LIVE
            out.setdefault(connector, {})[project_id] = status
    return out
