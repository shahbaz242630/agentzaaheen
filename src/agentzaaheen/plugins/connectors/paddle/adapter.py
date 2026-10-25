"""The real Paddle source: named read-only GETs, paged, mapped to SDK shapes.

It only ever calls five list endpoints (subscriptions, transactions,
adjustments, discounts, customers) through the ``JsonGetter`` the core hands it, so it
holds no key and opens no socket itself. Paging follows Paddle's own
``meta.pagination.next`` link and stops at a page cap rather than looping
forever on a misbehaving API.

Customers are read for one thing only: their names, which the caller turns
into fingerprints straight away (the customer-data check, ADR-023). Nothing
else about a customer is read, and the names are never part of a batch.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from agentzaaheen.core.sdk import (
    JsonGetter,
    Payment,
    Refund,
    RevenueBatch,
    SourceError,
    SourceErrorCode,
    Subscription,
)
from agentzaaheen.plugins.connectors.paddle import mapping
from agentzaaheen.plugins.connectors.paddle.mapping import Json

LIVE_BASE_URL = "https://api.paddle.com"
SANDBOX_BASE_URL = "https://sandbox-api.paddle.com"
ALLOWED_HOSTS = frozenset({"api.paddle.com", "sandbox-api.paddle.com"})
API_VERSION_HEADER = {"Paddle-Version": "1"}

MAX_PAGES = 200
_DISCOUNT_IDS_PER_CALL = 50
# Paddle's own per-page maximums for each list endpoint.
_PER_PAGE = {
    "/subscriptions": "200",
    "/transactions": "30",
    "/adjustments": "50",
    "/discounts": "200",
    "/customers": "200",
}


class PaddleSource:
    """Read-only revenue from one Paddle account. Implements the SDK's ``RevenueSource``."""

    def __init__(self, http: JsonGetter, *, max_pages: int = MAX_PAGES) -> None:
        self._http = http
        self._max_pages = max_pages

    @property
    def source(self) -> str:
        """The connector id."""
        return mapping.SOURCE

    async def fetch(self, *, payments_since: datetime | None, now: datetime) -> RevenueBatch:
        """Every subscription, plus transactions changed since ``payments_since``, plus all adjustments."""
        raw_subs = await self._pages("/subscriptions", {})
        discounts = await self._discounts(raw_subs)
        txn_params = {} if payments_since is None else {"updated_at[GTE]": _rfc3339(payments_since)}
        raw_txns = await self._pages("/transactions", txn_params)
        raw_adjs = await self._pages("/adjustments", {})
        try:
            subs: list[Subscription] = [mapping.subscription(s, discounts, now) for s in raw_subs]
            pays: list[Payment] = [p for t in raw_txns for p in mapping.payments(t)]
            refunds: list[Refund] = [r for a in raw_adjs if (r := mapping.refund(a)) is not None]
            return RevenueBatch(
                source=mapping.SOURCE,
                subscriptions=tuple(subs),
                payments=tuple(pays),
                refunds=tuple(refunds),
                fetched_at=now,
            )
        except ValidationError:
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "paddle data broke a revenue rule").at(
                "read paddle data"
            ) from None
        except SourceError as exc:
            exc.at("read paddle data")
            raise

    async def customer_names(self) -> list[str]:
        """Every customer's name, as Paddle holds it (customers without one are skipped).

        For fingerprinting only: the caller must not keep, log or show them.
        """
        raw = await self._pages("/customers", {})
        try:
            return [name for customer in raw if (name := mapping.optional_text(customer, "name"))]
        except SourceError as exc:
            exc.at("read paddle customers")
            raise

    async def _discounts(self, raw_subs: list[Json]) -> dict[str, Json]:
        ids = sorted(
            {
                mapping.text(mapping.obj(s["discount"], "discount"), "id")
                for s in raw_subs
                if s.get("discount") is not None
            }
        )
        found: dict[str, Json] = {}
        for start in range(0, len(ids), _DISCOUNT_IDS_PER_CALL):
            chunk = ids[start : start + _DISCOUNT_IDS_PER_CALL]
            for discount in await self._pages("/discounts", {"id": ",".join(chunk)}):
                found[mapping.text(discount, "id")] = discount
        return found

    async def _pages(self, path: str, params: Mapping[str, str]) -> list[Json]:
        out: list[Json] = []
        query = {**params, "per_page": _PER_PAGE[path]}
        try:
            for _ in range(self._max_pages):
                page = await self._http.get_json(path, query)
                out.extend(mapping.objects(page.get("data"), "data"))
                meta = mapping.obj(page.get("meta"), "meta")
                pagination = mapping.obj(meta.get("pagination"), "pagination")
                if pagination.get("has_more") is not True:
                    return out
                query = {**query, "after": _after_from(pagination.get("next"))}
            raise SourceError(SourceErrorCode.TOO_MUCH_DATA, f"more than {self._max_pages} pages from {path}")
        except SourceError as exc:
            exc.at(f"GET {path}")
            raise


def _after_from(next_url: object) -> str:
    """The ``after`` cursor inside Paddle's ``next`` link (we never follow the link itself)."""
    if not isinstance(next_url, str):
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "paddle said there is more but sent no next link")
    values = parse_qs(urlsplit(next_url).query).get("after", [])
    if len(values) != 1 or not values[0]:
        raise SourceError(SourceErrorCode.BAD_RESPONSE, "paddle's next link has no usable cursor")
    return values[0]


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
