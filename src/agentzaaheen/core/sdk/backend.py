"""Backend health: a hosted backend's state, services, disk and traffic (SDK 1.8.0, F03 slice 6).

Codes and numbers only. A vendor's free-text fields (error messages,
descriptions, names) are never kept: whoever controls the project can write
them, so they would be untrusted text on its way to a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PROJECT_REF = r"^[a-z0-9]{8,40}$"
_SERVICE = r"^[a-z_]{1,32}$"
MAX_SERVICES = 16


class ProjectState(StrEnum):
    """Where the hosted project is in its life. Anything the vendor adds later reads as ``unknown``."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"  # coming up, restoring, restarting, upgrading, resizing
    PAUSED = "paused"  # inactive, pausing, going down
    FAILED = "failed"  # init, restore or pause failed
    REMOVED = "removed"
    UNKNOWN = "unknown"


class ServiceHealth(BaseModel):
    """One service of the project (``db``, ``auth``, ``rest``...) and whether it is healthy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_SERVICE)
    healthy: bool


class BackendHealth(BaseModel):
    """One backend project, read once. ``disk_*`` and ``requests_24h`` are None if they could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    project_ref: str = Field(pattern=PROJECT_REF)
    state: ProjectState
    services: tuple[ServiceHealth, ...] = Field(max_length=MAX_SERVICES)
    disk_used_bytes: int | None = Field(default=None, ge=0)
    disk_size_bytes: int | None = Field(default=None, gt=0)
    requests_24h: int | None = Field(default=None, ge=0)
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def _disk_fits(self) -> BackendHealth:
        if (self.disk_used_bytes is None) != (self.disk_size_bytes is None):
            raise ValueError("disk used and disk size come together")
        used, size = self.disk_used_bytes, self.disk_size_bytes
        if used is not None and size is not None and used > size:
            raise ValueError("disk used cannot exceed the disk size")
        return self

    @property
    def healthy_services(self) -> int:
        """How many services are healthy."""
        return sum(service.healthy for service in self.services)

    @property
    def disk_basis_points(self) -> int | None:
        """Disk used, in hundredths of a percent (None if unknown)."""
        if self.disk_used_bytes is None or self.disk_size_bytes is None:
            return None
        return round(self.disk_used_bytes * 10_000 / self.disk_size_bytes)


class BackendSource(Protocol):
    """A connector that reads one backend project's health."""

    @property
    def source(self) -> str:
        """The connector id."""
        ...

    async def fetch(self, *, now: datetime) -> BackendHealth:
        """The project's health as of ``now``. Raises ``SourceError`` (or a subclass) only."""
        ...


class JsonReader(Protocol):
    """Read-only JSON GETs that may answer with an object or an array (the core's one HTTP client)."""

    async def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """The JSON object at ``path``. Raises ``SourceError`` (or a subclass) only."""
        ...

    async def get_json_list(self, path: str, params: Mapping[str, str]) -> list[object]:
        """The JSON array at ``path``. Raises ``SourceError`` (or a subclass) only."""
        ...
