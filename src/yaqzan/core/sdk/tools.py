"""Tool declarations: what a tool is allowed to do, as the policy gate sees it.

Every tool a plugin offers is declared once (``06-CONTRACTS`` §6). The gate
decides only from these declarations and from server state, never from what a
model says about a tool.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yaqzan.core.sdk.models import DataClass, Trust


class ToolLevel(StrEnum):
    """The permission ladder. Higher = more dangerous.

    read: looks only. draft: prepares something inside Yaqzan for a human.
    act: would change the outside world (only ever after human approval).
    forbidden: never callable, by anyone (delete, deploy, merge, billing).
    """

    READ = "read"
    DRAFT = "draft"
    ACT = "act"
    FORBIDDEN = "forbidden"

    @property
    def rank(self) -> int:
        """Position on the ladder (higher = more dangerous)."""
        return _LEVEL_ORDER.index(self)


_LEVEL_ORDER: tuple[ToolLevel, ...] = (ToolLevel.READ, ToolLevel.DRAFT, ToolLevel.ACT, ToolLevel.FORBIDDEN)


class ToolSpec(BaseModel):
    """One tool's declaration.

    ``outbound`` marks a free-form channel to the outside world (web search,
    fetch a URL, send): the third leg of the "lethal trifecta" (SECURITY §6.3).
    ``result_trust`` is UNTRUSTED unless the tool only returns numbers our own
    code computed; connector results are always untrusted.
    ``target_kind`` is set when the tool works on one object (a ticket, a PR).
    Such a tool takes that object **only** as the gate-checked target, never
    as an id inside its arguments; draft and act tools must always have one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=80)
    level: ToolLevel
    output_data_class: DataClass
    outbound: bool = False
    result_trust: Trust = Trust.UNTRUSTED
    target_kind: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def _writes_need_a_target(self) -> ToolSpec:
        if self.level in {ToolLevel.DRAFT, ToolLevel.ACT} and self.target_kind is None:
            raise ValueError("draft and act tools must declare the target_kind they act on")
        return self
