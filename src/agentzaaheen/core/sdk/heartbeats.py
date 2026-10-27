"""Heartbeats: how each Healthchecks check stands (SDK 1.13.0, F11 slice 5a).

A Healthchecks check expects a ping on a schedule: the external checker pings
each product's check while the product answers, and a job pings its own when
it ran. A check that stops hearing pings turns ``down`` and Healthchecks
alerts the phone itself. A connector only ever reports each check's **slug**
(our own short name), its **status** and when it last heard a ping: never its
name, description or ping address.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CHECK_SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"
MAX_SLUG = 64
MAX_CHECKS = 200


class CheckStatus(StrEnum):
    """A check's status, as Healthchecks says it."""

    NEW = "new"  # made, never pinged
    UP = "up"
    GRACE = "grace"  # a ping is late, inside the grace time: not down yet
    DOWN = "down"
    PAUSED = "paused"  # nobody is told if it stops


class HeartbeatCheck(BaseModel):
    """One check: its slug, status and last ping (``None``: never pinged)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(pattern=CHECK_SLUG, max_length=MAX_SLUG)
    status: CheckStatus
    last_ping: AwareDatetime | None = None


class HeartbeatReport(BaseModel):
    """Every check of the account, read once (an account with no checks reads as empty)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = "healthchecks"
    checks: tuple[HeartbeatCheck, ...] = Field(max_length=MAX_CHECKS)
    fetched_at: AwareDatetime
