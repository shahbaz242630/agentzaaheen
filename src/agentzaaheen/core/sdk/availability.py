"""Availability history: how many external checks found each project up, hour by hour (SDK 1.15.0).

The external checker (``apps/checker``) checks every address about once a
minute and adds each answer to an hourly count per address in its own
database. A connector reads those counts summed per project and hour, so the
availability objectives (ADR-032) can be worked out over 28 days: never an
address, only a project id, the hour and two numbers.

Reading SQL is a ``POST`` on Cloudflare's API, so a connector gets a
:class:`JsonQuerier`, not a ``JsonGetter``: the core sends only queries the
connector named when its client was made, and the key itself can only read.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PROJECT_ID = r"^[a-z0-9]+(-[a-z0-9]+)*$"
MAX_HOURS = 20_000  # 28 days for 29 projects; more than one read should ever be


class UptimeHour(BaseModel):
    """One project's checks in one hour: how many there were and how many found it up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str = Field(pattern=PROJECT_ID, max_length=64)
    hour: AwareDatetime  # the hour's start
    total: int = Field(ge=1)
    good: int = Field(ge=0)

    @model_validator(mode="after")
    def _whole_hour_and_good_within_total(self) -> Self:
        if self.good > self.total:
            raise ValueError("more good checks than checks")
        if (self.hour.minute, self.hour.second, self.hour.microsecond) != (0, 0, 0):
            raise ValueError("an hour starts on the hour")
        return self


class UptimeHistory(BaseModel):
    """Every project's hourly counts from ``since`` on, read once (a quiet checker reads as empty)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = "checker"
    since: AwareDatetime
    hours: tuple[UptimeHour, ...] = Field(max_length=MAX_HOURS)
    fetched_at: AwareDatetime


class JsonQuerier(Protocol):
    """A read-only SQL query, given to a connector by the core (the one HTTP client)."""

    async def query_json(self, path: str, sql: str, params: Sequence[int | str]) -> dict[str, object]:
        """Send ``sql`` with ``params`` to ``path``; return the decoded JSON object.

        Only a query named when the client was made is sent. Raises
        :class:`SourceError` (or a subclass) only.
        """
        ...
