"""The dashboard's registry switches on this laptop, kept in the laptop database (ADR-030, F06 slice 2b).

``tools.dashboard --switches`` uses :func:`connect` before anything is read: it
sets up the laptop database (``tools.laptop_db``) and says how every item
stands, so a connector that is switched off is not read at all. While the
dashboard runs, :class:`LaptopSwitches` serves the Customize page's switches
and the passkey approvals (ADR-SEC-007), and tells the chat, before every
question, what it may use. Its connection pool opens on first use, inside the
web server's own event loop; the passkey's open challenges live with it, in
memory, for the life of the dashboard.

There is one tenant on the laptop: :data:`LAPTOP_TENANT`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from pydantic import SecretStr

from tools import laptop_db
from yaqzan.core.api.registry import DashboardSwitches, PasskeyStatus, RegistryPage, Use
from yaqzan.core.auth import Site
from yaqzan.core.policy import PasskeyApprovals, RegistryControl, Standing, Verdict
from yaqzan.core.registry import Kind, Registry
from yaqzan.core.store import Database, DatabaseSettings, StoreError

LAPTOP_TENANT = uuid5(UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"), "yaqzan:laptop")  # the URL namespace
START_LAPTOP_DB = "uv run python -m tools.laptop_db up"

Prepare = Callable[[], Awaitable[SecretStr | None]]


def connect(registry: Registry, prepare: Prepare = laptop_db.prepare) -> tuple[SecretStr, Standing] | None:
    """The laptop database's URL and how every item stands now, or ``None`` (after saying why)."""

    async def read() -> tuple[SecretStr, Standing] | None:
        url = await prepare()
        if url is None:
            return None
        db = await Database.open(DatabaseSettings(dsn=url, min_size=1, max_size=1))
        try:
            return url, await RegistryControl(db, LAPTOP_TENANT, registry).standing()
        finally:
            await db.close()

    try:
        found = asyncio.run(read())
    except StoreError as exc:
        print(f"dashboard: the laptop database could not be used ({exc.code.value}).")
        return None
    if found is None:
        print(f"dashboard: the laptop database is not running. Start it with: {START_LAPTOP_DB}")
    return found


def off_connectors(standing: Standing, registry: Registry) -> frozenset[str]:
    """The connectors that are not on, so they are not read."""
    return frozenset(c.id for c in registry.connectors) - standing.on(Kind.CONNECTOR)


class LaptopSwitches:
    """The Customize page's switches and the chat's view of them, over one lazily opened pool."""

    def __init__(  # noqa: PLR0913  # the database, what exists, where it is used, the time, the passkey site
        self,
        url: SecretStr,
        registry: Registry,
        uses: Mapping[str, Mapping[str, Use]],
        clock: Callable[[], datetime],
        *,
        site: Site,
        enrol_code: str | None = None,
    ) -> None:
        self._settings = DatabaseSettings(dsn=url, min_size=1, max_size=4)
        self._site = site
        self._enrol_code = enrol_code
        self._registry = registry
        self._uses = uses
        self._clock = clock
        self._switches: DashboardSwitches | None = None
        self._opening: asyncio.Lock | None = None

    async def _open(self) -> DashboardSwitches:
        if self._opening is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if self._switches is None:
                db = await Database.open(self._settings)
                control = RegistryControl(db, LAPTOP_TENANT, self._registry)
                passkeys = PasskeyApprovals(
                    db, LAPTOP_TENANT, control, self._site, enrol_code=self._enrol_code, clock=self._clock
                )
                self._switches = DashboardSwitches(control, passkeys, self._registry, self._uses, self._clock)
        return self._switches

    async def standing(self) -> Standing:
        """How every item stands now (the chat asks before every question)."""
        return await (await self._open()).standing()

    async def page(self) -> RegistryPage:
        """The Customize page as stored now."""
        return await (await self._open()).page()

    async def switch_off(self, kind: Kind, item_id: str) -> Verdict:
        """Switch one item off."""
        return await (await self._open()).switch_off(kind, item_id)

    async def request_on(self, kind: Kind, item_id: str) -> Verdict:
        """Ask for one item to be switched on."""
        return await (await self._open()).request_on(kind, item_id)

    async def withdraw(self, request_id: UUID) -> Verdict:
        """Withdraw a waiting request."""
        return await (await self._open()).withdraw(request_id)

    async def refuse(self, request_id: UUID) -> Verdict:
        """Refuse a waiting request."""
        return await (await self._open()).refuse(request_id)

    async def approve(self, request_id: UUID, credential: dict[str, Any]) -> Verdict:
        """Approve with a Windows Hello signature for this one request."""
        return await (await self._open()).approve(request_id, credential)

    async def approve_options(self, request_id: UUID) -> dict[str, Any]:
        """Options for Windows Hello to sign for this one request."""
        return await (await self._open()).approve_options(request_id)

    async def passkey(self) -> PasskeyStatus:
        """Whether the passkey exists and may be set up now."""
        return await (await self._open()).passkey()

    async def enrol_options(self, code: str) -> dict[str, Any]:
        """Options to make the passkey (needs the start-up code)."""
        return await (await self._open()).enrol_options(code)

    async def enrol(self, code: str, credential: dict[str, Any]) -> None:
        """Keep the new passkey's public half."""
        await (await self._open()).enrol(code, credential)
