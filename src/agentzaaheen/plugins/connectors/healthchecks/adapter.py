"""The real Healthchecks source: one read-only GET of every check, mapped to ``HeartbeatReport``.

Only each check's slug, status and last ping are kept. A check with no slug,
or a status this code does not know, is left out: the Monitoring agent then
says the check it expected is missing, so nothing is ever counted as fine
that was not read as fine.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from agentzaaheen.core.sdk import (
    CheckStatus,
    HeartbeatCheck,
    HeartbeatReport,
    JsonGetter,
    SourceError,
    SourceErrorCode,
)

SOURCE = "healthchecks"
BASE_URL = "https://healthchecks.io"
ALLOWED_HOSTS = frozenset({"healthchecks.io"})
API_HEADERS = {"Accept": "application/json"}
CHECKS_PATH = "/api/v3/checks/"
KEY_HEADER = "X-Api-Key"
MAX_CHECKS = 200  # the SDK's limit for one report
_STATUSES = {status.value: status for status in CheckStatus}


class HealthchecksSource:
    """Every check of one Healthchecks project, read with its read-only key."""

    def __init__(self, http: JsonGetter) -> None:
        self._http = http

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    async def fetch(self, *, now: datetime) -> HeartbeatReport:
        """Every check, as of ``now``."""
        body = await self._http.get_json(CHECKS_PATH, {})
        raw = body.get("checks")
        if not isinstance(raw, list):
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "the answer has no list of checks")
        if len(raw) > MAX_CHECKS:
            raise SourceError(SourceErrorCode.TOO_MUCH_DATA, "more checks than one project should have")
        checks = tuple(check for item in raw if (check := _check(item)) is not None)
        return HeartbeatReport(checks=checks, fetched_at=now)


def _check(item: object) -> HeartbeatCheck | None:
    if not isinstance(item, dict):
        return None
    status = _STATUSES.get(str(item.get("status")))
    if status is None:
        return None
    try:
        return HeartbeatCheck(slug=item.get("slug"), status=status, last_ping=_when(item.get("last_ping")))
    except ValidationError:  # no slug, or not one of our short names
        return None


def _when(value: object) -> datetime | None:
    """A ping time with its zone, else ``None`` (never pinged, or not a time we can trust)."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None
