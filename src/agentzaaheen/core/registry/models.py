"""The registry's shapes: one connector, one skill, one plugin bundle, and what could not be read.

Every item carries a ``fingerprint``: the SHA-256 of all its files, so an
approval can be tied to exactly what was reviewed and lapse when anything
changes (research/07 §3.5). Ids are unique within each kind: a clash is
refused, never resolved by letting one silently replace the other.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentzaaheen.core.sdk import ToolLevel

ITEM_ID = r"^[a-z0-9]+(-[a-z0-9]+)*$"
FINGERPRINT = r"^[0-9a-f]{64}$"
_CODE = r"^[a-z0-9_]+(\.[a-z0-9_]+)*$"


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Kind(StrEnum):
    """The three kinds of item the registry holds."""

    CONNECTOR = "connector"
    SKILL = "skill"
    PLUGIN = "plugin"


class Invocation(StrEnum):
    """Who may start a skill: the model when the task fits (auto), or only a person naming it (manual)."""

    AUTO = "auto"
    MANUAL = "manual"


class Layout(StrEnum):
    """Which app's bundle layout a plugin folder uses (its ``.<app>-plugin/plugin.json``)."""

    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"


class Connector(_Shape):
    """A data source, from its reviewed ``plugin.yaml``.

    ``level`` is the highest permission the manifest declares; a manifest that
    declares none counts as ``act``, so it can only ever propose. ``key_slots``
    are vault slot names, never values.
    """

    id: str = Field(pattern=ITEM_ID, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    level: ToolLevel
    hosts: tuple[str, ...]
    endpoints: tuple[str, ...]
    key_slots: tuple[str, ...]
    fingerprint: str = Field(pattern=FINGERPRINT)


class Skill(_Shape):
    """A written playbook (``SKILL.md``). ``requires`` lists the connector ids it needs."""

    id: str = Field(pattern=ITEM_ID, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    invocation: Invocation
    requires: tuple[str, ...]
    fingerprint: str = Field(pattern=FINGERPRINT)


class Plugin(_Shape):
    """A bundle folder that ships skills, connectors, commands and agents as one unit."""

    id: str = Field(pattern=ITEM_ID, max_length=64)
    description: str = Field(max_length=1024)
    version: str | None = Field(default=None, max_length=40)
    layout: Layout
    skills: int = Field(ge=0)
    connectors: int = Field(ge=0)
    commands: int = Field(ge=0)
    agents: int = Field(ge=0)
    hooks: bool
    fingerprint: str = Field(pattern=FINGERPRINT)


class Problem(_Shape):
    """One item that could not be read, where it is, and why in plain English."""

    kind: Kind
    path: str = Field(min_length=1, max_length=300)
    code: str = Field(pattern=_CODE)
    message: str = Field(min_length=1, max_length=300)


class Registry(_Shape):
    """Everything that was read, sorted by id, plus everything that was refused."""

    connectors: tuple[Connector, ...] = ()
    skills: tuple[Skill, ...] = ()
    plugins: tuple[Plugin, ...] = ()
    problems: tuple[Problem, ...] = ()

    @model_validator(mode="after")
    def _unique_ids(self) -> Registry:
        for kind, items in (
            (Kind.CONNECTOR, self.connectors),
            (Kind.SKILL, self.skills),
            (Kind.PLUGIN, self.plugins),
        ):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{kind} ids must be unique")
        return self
