"""Check one project's addresses, one at a time, and judge each by its rule."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from agentzaaheen.core.sdk import SiteRule, SourceError, StatusGetter, UptimeReport, check_site

SOURCE = "uptime"


class UptimeSource:
    """Checks every address of one project through the core's ``StatusGetter``.

    An address that does not answer counts as down; it never stops the others
    being checked.
    """

    def __init__(self, getter: StatusGetter, rules: Sequence[SiteRule]) -> None:
        if not rules:
            raise ValueError("an uptime source needs at least one address")
        self._getter = getter
        self._rules = tuple(rules)

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    async def fetch(self, *, now: datetime) -> UptimeReport:
        """Every address checked once, as of ``now``."""
        checks = []
        for rule in self._rules:
            try:
                answer = await self._getter.status_of(rule.url)
            except SourceError:
                answer = None
            checks.append(check_site(rule, answer, now))
        return UptimeReport(sites=tuple(checks), fetched_at=now)
