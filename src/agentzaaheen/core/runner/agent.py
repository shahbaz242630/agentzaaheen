"""What an agent is to the runner, and the ticket only the runner can give it (ADR-036, F11 slice 1).

An agent is any object with a role, a tool list, a budget and an async
``run(ticket)``. It cannot be started without a :class:`RunTicket`, and a
ticket can only be made inside this package (:func:`_mint`): making one
anywhere else raises :class:`RunnerOnlyError`, and a test fails if any code outside
``core/runner`` even tries. So every run passes the runner's checkpoints.

What an agent hands back is an :class:`AgentResult`: finding drafts only. The
runner adds the tenant, the workspace and the run before anything is saved,
so an agent cannot write into another workspace or claim another run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentzaaheen.core.policy import Budget
from agentzaaheen.core.sdk import DataClass, Evidence, FindingKind, Severity, Trust

MAX_FINDINGS = 50  # a run that "finds" more than this is not working; the rest would be noise

_RUNNER = object()  # the runner's own seal: only this package holds it


class RunnerOnlyError(RuntimeError):
    """A run ticket was made outside the runner."""


@dataclass(frozen=True)
class RunTicket:
    """One run's facts, handed to the agent by the runner: never made by anyone else.

    ``pack`` is the agent's **frozen** role pack (the copy stored when the role
    was given to the workspace), not the live one in the library.
    """

    run_id: UUID
    workspace_id: UUID
    agent_id: UUID
    role_id: str
    version: str
    pack: Mapping[str, str]
    tools: frozenset[str]
    budget: Budget
    started_at: datetime
    seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _RUNNER:
            raise RunnerOnlyError("only the runner starts an agent run")


def _mint(  # noqa: PLR0913  # each is a separate fact of the run
    *,
    run_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    role_id: str,
    version: str,
    pack: Mapping[str, str],
    tools: frozenset[str],
    budget: Budget,
    started_at: datetime,
) -> RunTicket:
    """A ticket with the runner's seal (for ``core.runner.run`` only)."""
    return RunTicket(
        run_id, workspace_id, agent_id, role_id, version, dict(pack), tools, budget, started_at, _RUNNER
    )


class FindingDraft(BaseModel):
    """A finding as an agent writes it: the runner adds where and which run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FindingKind
    severity: Severity
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(max_length=20_000)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    metric_refs: dict[str, str] = Field(default_factory=dict)
    trust: Trust
    data_class: DataClass


class AgentResult(BaseModel):
    """What one run found. No findings means "nothing found", never "did not run"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[FindingDraft, ...] = Field(default=(), max_length=MAX_FINDINGS)


class AgentWork(Protocol):
    """An agent, as the runner sees it."""

    @property
    def role_id(self) -> str:
        """The role it does: must be the role of the agent it runs as."""
        ...

    @property
    def tools(self) -> frozenset[str]:
        """The tools it may ask for (at most 6; checked against its role's permission)."""
        ...

    @property
    def budget(self) -> Budget:
        """Its hard limits for one run (steps, tokens, seconds)."""
        ...

    async def run(self, ticket: RunTicket) -> AgentResult:
        """Do one run's work and say what it found."""
        ...
