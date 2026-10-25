"""What the Customize page is sent: the registry, plus where each connector is in use (F06).

The registry says what exists; ``uses`` says, for each connector, which
projects it serves and whether it was read this run. A skill lists any
connector it needs that is not in the registry, so the page can say so. The
UI checks these shapes with the same rules in zod.

Each item carries its ``state`` (ADR-030). With a database, the page also
carries the requests waiting for approval and ``switches: true``, and
:class:`DashboardSwitches` lets the dashboard switch an item off, ask for it
to be switched on, or withdraw that request, always as the Chairman on the
dashboard channel (there is no login yet; it serves this laptop only). It
also sets up the Chairman's passkey once and approves or refuses requests;
approving needs a Windows Hello signature for that one request
(``policy.PasskeyApprovals``, ADR-SEC-007).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentzaaheen.core.api.views import ProjectDetail
from agentzaaheen.core.metrics import Freshness
from agentzaaheen.core.policy import (
    OUTSIDE_KINDS,
    Channel,
    PasskeyApprovals,
    RegistryControl,
    Role,
    Standing,
    Verdict,
)
from agentzaaheen.core.registry import Invocation, Kind, Layout, Registry, State, state_of
from agentzaaheen.core.sdk import ToolLevel

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
    state: State


class SkillView(_View):
    """A skill card. ``missing`` are needed connectors the registry does not have."""

    id: str = Field(pattern=_ITEM)
    description: str
    invocation: Invocation
    requires: tuple[str, ...]
    missing: tuple[str, ...]
    fingerprint: str = Field(pattern=_HASH)
    state: State


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
    state: State


class RequestView(_View):
    """A request to switch an item on, waiting for approval. ``stale``: its files changed since."""

    id: UUID
    kind: Kind
    item_id: str = Field(pattern=_ITEM)
    requested_at: datetime
    stale: bool


class ItemRef(_View):
    """Which item a switch or request is about (sent by the page)."""

    kind: Kind
    id: str = Field(pattern=_ITEM, max_length=64)


class PasskeyStatus(_View):
    """Whether the Chairman's passkey exists, and whether it may be set up on this run."""

    enrolled: bool
    enrolment_open: bool


class EnrolCode(_View):
    """The one-time code printed when the dashboard was started with ``--enrol-passkey``."""

    code: str = Field(pattern=r"^[0-9a-f]{8}$")


class EnrolAnswer(EnrolCode):
    """The code again, and the browser's new passkey (``credential.toJSON()``)."""

    credential: dict[str, Any]


class SignedAnswer(_View):
    """The browser's signed answer for one request (``credential.toJSON()``)."""

    credential: dict[str, Any]


class PasskeyOptions(_View):
    """What the browser hands to ``navigator.credentials`` (WebAuthn options as JSON)."""

    options: dict[str, Any]


class ProblemView(_View):
    """An item that could not be read, and why."""

    kind: Kind
    path: str
    message: str


class RegistryPage(_View):
    """Everything the Customize page shows. ``switches``: on/off can be changed (a database is connected)."""

    connectors: tuple[ConnectorView, ...] = ()
    skills: tuple[SkillView, ...] = ()
    plugins: tuple[PluginView, ...] = ()
    problems: tuple[ProblemView, ...] = ()
    switches: bool = False
    requests: tuple[RequestView, ...] = ()


def registry_page(
    registry: Registry, uses: Mapping[str, Mapping[str, Use]], standing: Standing | None = None
) -> RegistryPage:
    """The page for ``registry``; ``uses`` maps connector id → project id → how it stands.

    Without ``standing`` (no database) every item shows its default state and nothing can be switched.
    """
    known = {connector.id for connector in registry.connectors}

    def state(kind: Kind, item_id: str, fingerprint: str) -> State:
        if standing is not None:
            return standing.states[(kind, item_id)]
        return state_of(kind, item_id, fingerprint, {}, outside=kind in OUTSIDE_KINDS)

    return RegistryPage(
        connectors=tuple(
            ConnectorView(
                **c.model_dump(),
                uses=tuple(ProjectUse(project_id=p, status=s) for p, s in uses.get(c.id, {}).items()),
                state=state(Kind.CONNECTOR, c.id, c.fingerprint),
            )
            for c in registry.connectors
        ),
        skills=tuple(
            SkillView(
                **s.model_dump(),
                missing=tuple(r for r in s.requires if r not in known),
                state=state(Kind.SKILL, s.id, s.fingerprint),
            )
            for s in registry.skills
        ),
        plugins=tuple(
            PluginView(**p.model_dump(), state=state(Kind.PLUGIN, p.id, p.fingerprint))
            for p in registry.plugins
        ),
        problems=tuple(ProblemView(kind=p.kind, path=p.path, message=p.message) for p in registry.problems),
        switches=standing is not None,
        requests=tuple(
            RequestView(id=r.id, kind=r.kind, item_id=r.item_id, requested_at=r.requested_at, stale=r.stale)
            for r in (standing.pending if standing else ())
        ),
    )


class DashboardSwitches:
    """The Customize page's switches: the dashboard on this laptop acts as the Chairman (no login yet)."""

    def __init__(
        self,
        control: RegistryControl,
        passkeys: PasskeyApprovals,
        registry: Registry,
        uses: Mapping[str, Mapping[str, Use]],
        clock: Callable[[], datetime],
    ) -> None:
        self._control = control
        self._passkeys = passkeys
        self._registry = registry
        self._uses = uses
        self._clock = clock

    async def standing(self) -> Standing:
        """How every item stands now."""
        return await self._control.standing()

    async def page(self) -> RegistryPage:
        """The page with the states and requests as stored now."""
        return registry_page(self._registry, self._uses, await self._control.standing())

    async def switch_off(self, kind: Kind, item_id: str) -> Verdict:
        """Switch one item off."""
        return await self._control.switch_off(
            kind, item_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
        )

    async def request_on(self, kind: Kind, item_id: str) -> Verdict:
        """Ask for one item to be switched on (approval comes later, with a passkey)."""
        return await self._control.request_on(
            kind, item_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
        )

    async def withdraw(self, request_id: UUID) -> Verdict:
        """Withdraw a waiting request."""
        return await self._control.withdraw(
            request_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
        )

    async def passkey(self) -> PasskeyStatus:
        """Whether the passkey exists and may be set up now."""
        enrolled, open_now = await self._passkeys.status()
        return PasskeyStatus(enrolled=enrolled, enrolment_open=open_now)

    async def enrol_options(self, code: str) -> dict[str, Any]:
        """Options to make the passkey (needs the start-up code)."""
        return await self._passkeys.enrol_options(code)

    async def enrol(self, code: str, credential: dict[str, Any]) -> None:
        """Keep the new passkey's public half."""
        await self._passkeys.enrol(code, credential)

    async def approve_options(self, request_id: UUID) -> dict[str, Any]:
        """Options for Windows Hello to sign for this one request."""
        return await self._passkeys.approve_options(request_id)

    async def approve(self, request_id: UUID, credential: dict[str, Any]) -> Verdict:
        """Approve with a signed answer; the item switches on if it holds."""
        return await self._passkeys.approve(request_id, credential)

    async def refuse(self, request_id: UUID) -> Verdict:
        """Refuse a waiting request."""
        return await self._control.refuse(
            request_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
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
