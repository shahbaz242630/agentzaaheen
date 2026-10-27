"""The real availability source: one read-only SQL query on the checker's database, as ``UptimeHistory``.

The checker keeps, per address and hour, how many checks ran and how many
found it up (``apps/checker/schema.sql``, table ``hourly``, 90 days). This
source asks for those counts summed per project and hour from ``since`` on,
through Cloudflare's D1 query API with a key that can only read. The query is
fixed here; the core sends nothing else. Any row that is not exactly what the
checker writes fails the whole read: a count is never guessed or skipped.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import ValidationError

from agentzaaheen.core.sdk import JsonQuerier, SourceError, SourceErrorCode, UptimeHistory, UptimeHour
from agentzaaheen.core.sdk.availability import MAX_HOURS

SOURCE = "availability"
BASE_URL = "https://api.cloudflare.com"
ALLOWED_HOSTS = frozenset({"api.cloudflare.com"})
API_HEADERS = {"Accept": "application/json"}
KEY_HEADER = "Authorization"  # "Bearer <key>"
HOURS_QUERY = (
    "SELECT project, hour, SUM(total) AS total, SUM(good) AS good FROM hourly "
    "WHERE hour >= ?1 GROUP BY project, hour ORDER BY hour, project"
)
READ_QUERIES = frozenset({HOURS_QUERY})
ACCOUNT_ID = r"^[0-9a-f]{32}$"
DATABASE_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_HOUR_S = 3600


def query_path(account: str, database: str) -> str:
    """The D1 query path for one account and database (both checked, so nothing else fits in)."""
    if not re.fullmatch(ACCOUNT_ID, account) or not re.fullmatch(DATABASE_ID, database):
        raise ValueError("the account and database ids must be Cloudflare's hex ids")
    return f"/client/v4/accounts/{account}/d1/database/{database}/query"


class AvailabilitySource:
    """Every project's hourly check counts from the checker's database, read with a read-only key."""

    def __init__(self, http: JsonQuerier, *, account: str, database: str) -> None:
        self._http = http
        self._path = query_path(account, database)

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    async def fetch(self, *, since: datetime, now: datetime) -> UptimeHistory:
        """Every project's counts for each hour starting at or after ``since``, as of ``now``."""
        first = int(since.timestamp()) // _HOUR_S
        body = await self._http.query_json(self._path, HOURS_QUERY, [first])
        rows = _rows(body)
        if len(rows) > MAX_HOURS:
            raise SourceError(SourceErrorCode.TOO_MUCH_DATA, "more hourly counts than one read should have")
        hours = tuple(_hour(row) for row in rows)
        return UptimeHistory(since=datetime.fromtimestamp(first * _HOUR_S, UTC), hours=hours, fetched_at=now)


def _rows(body: dict[str, object]) -> list[object]:
    """The result rows of the one statement, or ``BAD_RESPONSE`` if the answer is not a success."""
    results = body.get("result")
    if body.get("success") is not True or not isinstance(results, list) or len(results) != 1:
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "the database did not answer the query")
    statement = results[0]
    if not isinstance(statement, dict) or statement.get("success") is not True:
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "the database did not answer the query")
    rows = statement.get("results")
    if not isinstance(rows, list):
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "the answer has no rows")
    return rows


def _hour(row: object) -> UptimeHour:
    if not isinstance(row, dict):
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "a row is not a record")
    hour = row.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or hour < 0:
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "a row has no hour")
    try:
        return UptimeHour(
            project=row.get("project"),
            hour=datetime.fromtimestamp(hour * _HOUR_S, UTC),
            total=row.get("total"),
            good=row.get("good"),
        )
    except (ValidationError, OverflowError, OSError, ValueError):
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "a row's counts are not usable") from None
