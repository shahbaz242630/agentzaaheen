"""Certificate and domain dates on this laptop, for the Monitoring agents (F11 slice 5b).

Certificates: every host in the uptime list (``projects/uptime.yaml``), read
with one TLS handshake each, at most every 6 hours. Domains: the local list
``projects/domains.yaml`` (git-ignored and path-guarded, Rulebook §7b), each
project's own .com/.net domains, read from the public registry at most once a
day::

    zaaheen: [zaaheen.com]
    sandoq-kin: [sanduqkin.com]

Both are read by code inside a Monitoring round that has an agent to wake; a
read that fails keeps the last one, which turns "not read" by its age. No key
is needed; only our own host and domain names leave this machine.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from agentzaaheen.core.net import CertProbe, JsonClient, NetPolicy
from agentzaaheen.core.sdk import HOST_NAME, ExpiryKind, ExpiryReport, SiteRule, SourceError, get_logger, say
from agentzaaheen.plugins.connectors import expiry
from tools.dashboard_sources import ListNotUsableError

CERTS_EVERY = timedelta(hours=6)
DOMAINS_EVERY = timedelta(days=1)
MAX_LIST_BYTES = 16 * 1024
MAX_DOMAINS = 10  # per project
MAX_NAME = 253  # the longest DNS name
_UNREAD_REASONS = frozenset({"cert.no_answer", "domain.no_answer", "domain.no_date"})

Clock = Callable[[], datetime]
Names = dict[str, tuple[str, ...]]


class DomainsListError(Exception):
    """The list could not be used; the message says why in plain words."""


def load_domains(path: Path, projects: Collection[str]) -> Names:
    """Each project's own domains from ``path``; only known projects and .com/.net names are allowed."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise DomainsListError(f"the domain list {path} could not be read") from None
    if len(raw) > MAX_LIST_BYTES:
        raise DomainsListError("the domain list is too big")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise DomainsListError("the domain list is not valid YAML") from None
    if not isinstance(data, dict) or not data:
        raise DomainsListError("the domain list must map each project to its domains")
    out: Names = {}
    for project, names in data.items():
        if project not in projects:
            raise DomainsListError(f"the domain list names an unknown project: {project!r}")
        if not isinstance(names, list) or not names or len(names) > MAX_DOMAINS:
            raise DomainsListError(f"{project}: give a list of 1 to {MAX_DOMAINS} domains")
        for name in names:
            if not isinstance(name, str) or len(name) > MAX_NAME or not re.fullmatch(HOST_NAME, name):
                raise DomainsListError(f"{project}: {name!r} is not a lower-case domain name")
            if name.rsplit(".", 1)[-1] not in expiry.RDAP_TLDS:
                raise DomainsListError(f"{project}: {name} is not a .com or .net domain")
        out[project] = tuple(dict.fromkeys(names))
    return out


def domains_list(path: Path | None, projects: Collection[str]) -> Names:
    """The local list given with ``--domains`` (empty without one); a broken list is said and raised."""
    if path is None:
        return {}
    log = get_logger("agentzaaheen.settings")
    try:
        found = load_domains(path, projects)
    except DomainsListError as exc:
        print(f"dashboard: {exc}.")  # the exact rule, on the console only: it can quote the file
        say(log, "settings_refused", setting="domains")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="domains", entries=len(found))
    return found


def cert_hosts(rules: Mapping[str, tuple[SiteRule, ...]]) -> Names:
    """Each project's hosts, from its uptime addresses (each host once)."""
    return {
        project: tuple(dict.fromkeys(urlsplit(rule.url).hostname or "" for rule in sites))
        for project, sites in rules.items()
    }


class ExpiryReader:
    """Reads one kind of end date at most once per ``every``; :attr:`latest` is the newest good read."""

    def __init__(
        self,
        kind: ExpiryKind,
        read: Callable[[datetime], Awaitable[ExpiryReport]],
        *,
        every: timedelta,
        clock: Clock,
    ) -> None:
        self.kind = kind
        self._read = read
        self._every = every
        self._clock = clock
        self._tried_at: datetime | None = None
        self._log = get_logger("agentzaaheen.expiry")
        self.latest: ExpiryReport | None = None

    async def refresh(self) -> None:
        """Read if the last try is older than ``every``; a failure is logged and the last read kept."""
        now = self._clock()
        if self._tried_at is not None and now - self._tried_at < self._every:
            return
        self._tried_at = now
        try:
            report = await self._read(now)
        except SourceError as exc:
            say(self._log, "expiry_read_failed", kind=self.kind.value, code=exc.code.value)
            return
        self.latest = report
        unread = sum(1 for check in report.checks if check.reason in _UNREAD_REASONS)
        say(self._log, "expiry_read_done", kind=self.kind.value, checks=len(report.checks), unread=unread)


def cert_reader(
    hosts: Mapping[str, tuple[str, ...]], *, clock: Clock, probe: CertProbe | None = None
) -> ExpiryReader | None:
    """Reads every listed host's certificate every 6 hours (None when no host is listed)."""
    every_host = tuple(dict.fromkeys(h for names in hosts.values() for h in names))
    if not every_host:
        return None
    getter = probe if probe is not None else CertProbe(frozenset(every_host))

    async def read(now: datetime) -> ExpiryReport:
        return await expiry.CertSource(getter, every_host).fetch(now=now)

    return ExpiryReader(ExpiryKind.CERT, read, every=CERTS_EVERY, clock=clock)


def domain_reader(
    domains: Mapping[str, tuple[str, ...]], *, clock: Clock, transport: httpx.AsyncBaseTransport | None = None
) -> ExpiryReader | None:
    """Reads every listed domain's end date once a day (None when no domain is listed)."""
    every_domain = tuple(dict.fromkeys(d for names in domains.values() for d in names))
    if not every_domain:
        return None

    async def read(now: datetime) -> ExpiryReport:
        async with JsonClient(
            expiry.RDAP_URL,
            policy=NetPolicy(allowed_hosts=expiry.ALLOWED_HOSTS),
            headers=expiry.API_HEADERS,
            transport=transport,
        ) as http:
            return await expiry.DomainSource(http, every_domain).fetch(now=now)

    return ExpiryReader(ExpiryKind.DOMAIN, read, every=DOMAINS_EVERY, clock=clock)
