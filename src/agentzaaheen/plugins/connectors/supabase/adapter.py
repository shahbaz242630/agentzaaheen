"""The real Supabase source: four named read-only GETs for one project, mapped to ``BackendHealth``.

It reads the project (its state), its services' health (asking once more
about any that looked unhealthy, so a passing blip does not show red), its disk
utilisation (not for a paused project, which has neither) and the last day's
API counts, through the ``JsonReader`` the core hands it, so it holds no token
and opens no socket itself. The state is required; disk and usage are extras:
if either cannot be read, the card says "unknown" for it instead of failing the
whole project.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import datetime

from agentzaaheen.core.sdk import (
    PROJECT_REF,
    BackendHealth,
    JsonReader,
    ProjectState,
    ServiceHealth,
    SourceError,
    SourceErrorCode,
)
from agentzaaheen.plugins.connectors.supabase import mapping

BASE_URL = "https://api.supabase.com"
ALLOWED_HOSTS = frozenset({"api.supabase.com"})
API_HEADERS = {"Accept": "application/json"}


class SupabaseSource:
    """Read-only health of one Supabase project. Implements the SDK's ``BackendSource``."""

    def __init__(self, http: JsonReader, project_ref: str, *, unused: Collection[str] = ()) -> None:
        """``unused``: services this project switched off on purpose (e.g. ``rest``); never asked about."""
        if not re.fullmatch(PROJECT_REF, project_ref):
            raise ValueError("a Supabase project ref is 8-40 lower-case letters and digits")
        if not set(unused) <= set(mapping.SERVICES):
            raise ValueError(f"unused services must be among {', '.join(mapping.SERVICES)}")
        self._http = http
        self._ref = project_ref
        self._services = tuple(s for s in mapping.SERVICES if s not in set(unused))

    @property
    def source(self) -> str:
        """The connector id."""
        return mapping.SOURCE

    @property
    def project_ref(self) -> str:
        """The project this source reads."""
        return self._ref

    async def _confirmed(self, base: str, services: tuple[ServiceHealth, ...]) -> tuple[ServiceHealth, ...]:
        """Ask once more about any service that looked unhealthy: a single read can catch a passing blip.

        Seen live on 20 Oct 2026: realtime read "unhealthy" in the combined check and healthy straight after.
        """
        doubtful = [service.name for service in services if not service.healthy]
        if not doubtful:
            return services
        again = {
            s.name: s.healthy
            for s in mapping.services(
                await self._http.get_json_list(f"{base}/health", {"services": ",".join(doubtful)}), doubtful
            )
        }
        return tuple(
            ServiceHealth(name=s.name, healthy=again.get(s.name, False)) if not s.healthy else s
            for s in services
        )

    async def fetch(self, *, now: datetime) -> BackendHealth:
        """State, service health, disk and the last day's API requests, as of ``now``."""
        base = f"/v1/projects/{self._ref}"
        state = mapping.state(await self._http.get_json(base, {}))
        services: tuple[ServiceHealth, ...] = ()
        used: int | None = None
        size: int | None = None
        # A paused project has no services or disk to ask about: Supabase refuses both (seen live 28 Oct 2026,
        # "No active services available"), which read as "could not read" instead of "paused".
        if state is not ProjectState.PAUSED:
            health = await self._http.get_json_list(f"{base}/health", {"services": ",".join(self._services)})
            services = await self._confirmed(base, mapping.services(health, self._services))
            try:
                used, size = mapping.disk(await self._http.get_json(f"{base}/config/disk/util", {}))
            except SourceError as exc:
                if exc.code is SourceErrorCode.AUTH:
                    raise
        requests: int | None = None
        try:
            usage = await self._http.get_json(
                f"{base}/analytics/endpoints/usage.api-counts", {"interval": "1day"}
            )
            requests = mapping.requests(usage)
        except SourceError as exc:
            if exc.code is SourceErrorCode.AUTH:
                raise
        return BackendHealth(
            source=mapping.SOURCE,
            project_ref=self._ref,
            state=state,
            services=services,
            disk_used_bytes=used,
            disk_size_bytes=size,
            requests_24h=requests,
            fetched_at=now,
        )
