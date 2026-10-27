"""Certificate and domain end dates, one name at a time, mapped to ``ExpiryReport``.

Certificates are read through the core's ``CertGetter`` (one TLS handshake per
host). Domains are read from the public registry (RDAP) through the
``JsonGetter`` the core hands it: only the "expiration" event is kept. A name
that could not be read says so in its reason and never stops the others.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from agentzaaheen.core.sdk import (
    HOST_NAME,
    CertGetter,
    ExpiryCheck,
    ExpiryKind,
    ExpiryReport,
    JsonGetter,
    SourceError,
)

SOURCE = "expiry"
RDAP_URL = "https://rdap.verisign.com"
ALLOWED_HOSTS = frozenset({"rdap.verisign.com"})
API_HEADERS = {"Accept": "application/rdap+json, application/json"}
RDAP_TLDS = frozenset({"com", "net"})  # Verisign's registries; other endings are refused when listed
MAX_NAMES = 100


def _names(names: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(names))
    if len(unique) > MAX_NAMES:
        raise ValueError(f"at most {MAX_NAMES} names")
    for name in unique:
        if not re.fullmatch(HOST_NAME, name):
            raise ValueError(f"{name!r} is not a lower-case host name")
    return unique


class CertSource:
    """Each host's certificate: valid or not, and when it ends."""

    def __init__(self, getter: CertGetter, hosts: Sequence[str]) -> None:
        self._getter = getter
        self._hosts = _names(hosts)

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    async def fetch(self, *, now: datetime) -> ExpiryReport:
        """Every host read once, as of ``now``."""
        checks = []
        for host in self._hosts:
            try:
                answer = await self._getter.certificate_of(host)
            except SourceError:
                checks.append(ExpiryCheck(kind=ExpiryKind.CERT, name=host, reason="cert.no_answer"))
                continue
            checks.append(
                ExpiryCheck(
                    kind=ExpiryKind.CERT, name=host, expires_at=answer.not_after, reason=answer.reason
                )
            )
        return ExpiryReport(kind=ExpiryKind.CERT, checks=tuple(checks), fetched_at=now)


class DomainSource:
    """Each domain's registration end date, from the public registry."""

    def __init__(self, http: JsonGetter, domains: Sequence[str]) -> None:
        names = _names(domains)
        for name in names:
            if name.rsplit(".", 1)[-1] not in RDAP_TLDS:
                raise ValueError(f"{name}: only .com and .net domains can be read")
        self._http = http
        self._domains = names

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    async def fetch(self, *, now: datetime) -> ExpiryReport:
        """Every domain read once, as of ``now``."""
        checks = []
        for name in self._domains:
            tld = name.rsplit(".", 1)[-1]
            try:
                body = await self._http.get_json(f"/{tld}/v1/domain/{name}", {})
            except SourceError:
                checks.append(ExpiryCheck(kind=ExpiryKind.DOMAIN, name=name, reason="domain.no_answer"))
                continue
            ends = _expiration(body)
            reason = "domain.ok" if ends is not None else "domain.no_date"
            checks.append(ExpiryCheck(kind=ExpiryKind.DOMAIN, name=name, expires_at=ends, reason=reason))
        return ExpiryReport(kind=ExpiryKind.DOMAIN, checks=tuple(checks), fetched_at=now)


def _expiration(body: dict[str, object]) -> datetime | None:
    """The "expiration" event's date with its zone, else ``None``."""
    events = body.get("events")
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, dict) and event.get("eventAction") == "expiration":
            when = event.get("eventDate")
            if not isinstance(when, str):
                return None
            try:
                parsed = datetime.fromisoformat(when)
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else None
    return None
