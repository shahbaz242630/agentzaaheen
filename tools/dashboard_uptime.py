"""Uptime on this laptop: read the local address list, check each address once, show the cards (F03 slice 4).

The list lives in ``projects/uptime.yaml``, which is git-ignored and blocked
by the path guard (real project configuration never reaches the public repo,
Rulebook §7b). Each project maps to its addresses, each with a rule::

    zaaheen:
      - {url: "https://example.com", expect: ok}       # a page or a redirect
      - {url: "https://api.example.com", expect: answer}  # anything but a server error

Every check is a status-only GET through the core's ``StatusProbe``: the page
is never read, redirects are not followed, and only the listed hosts may be
reached.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import ValidationError

from yaqzan.core.api import Portfolio
from yaqzan.core.api.overlay import CardMaker, OverlayPortfolio
from yaqzan.core.api.uptime import CARD_ID, uptime_card
from yaqzan.core.api.views import Card
from yaqzan.core.net import StatusProbe
from yaqzan.core.sdk import SiteRule, UptimeReport
from yaqzan.plugins.connectors.uptime import UptimeSource

# Checked once at start: treated as an hourly source, so it shows "unknown" after two hours.
UPTIME_SCHEDULE = timedelta(hours=1)
MAX_LIST_BYTES = 64 * 1024

Clock = Callable[[], datetime]


class UptimeListError(Exception):
    """The address list could not be used; the message says why in plain words."""


def load_uptime(path: Path, projects: Collection[str]) -> dict[str, tuple[SiteRule, ...]]:
    """Each project's addresses from ``path``; only projects in ``projects`` are allowed."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise UptimeListError(f"the uptime list {path} could not be read") from None
    if len(raw) > MAX_LIST_BYTES:
        raise UptimeListError("the uptime list is too big")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise UptimeListError("the uptime list is not valid YAML") from None
    if not isinstance(data, dict) or not data:
        raise UptimeListError("the uptime list must map each project to its addresses")
    out: dict[str, tuple[SiteRule, ...]] = {}
    for project, sites in data.items():
        if project not in projects:
            raise UptimeListError(f"the uptime list names an unknown project: {project!r}")
        if not isinstance(sites, list) or not sites:
            raise UptimeListError(f"{project}: give a list of addresses")
        try:
            out[project] = tuple(SiteRule.model_validate(site) for site in sites)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise UptimeListError(f"{project}: an address is not usable ({first['msg']})") from None
    return out


async def read_uptime(
    rules: Mapping[str, tuple[SiteRule, ...]],
    now: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, UptimeReport]:
    """Every project's addresses checked once, through one probe that may reach only the listed hosts."""
    hosts = frozenset(urlsplit(rule.url).hostname or "" for sites in rules.values() for rule in sites)
    async with StatusProbe(hosts, transport=transport) as probe:
        return {project: await UptimeSource(probe, sites).fetch(now=now) for project, sites in rules.items()}


def uptime_maker(report: UptimeReport, clock: Clock) -> CardMaker:
    """Builds one project's Uptime card, judging freshness each time it is shown."""

    def make() -> Card:
        return uptime_card(report, schedule=UPTIME_SCHEDULE, now=clock())

    return make


def uptime_portfolio(
    base: Portfolio,
    rules: Mapping[str, tuple[SiteRule, ...]],
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Portfolio:
    """``base`` with each listed project's real Uptime card, after saying how each project stood."""
    count = sum(len(sites) for sites in rules.values())
    print(f"dashboard: checking {count} addresses for uptime (status only)...")
    reports = asyncio.run(read_uptime(rules, clock(), transport))
    for project, report in reports.items():
        down = [site.url for site in report.sites if not site.up]
        note = f"; down: {', '.join(down)}" if down else ""
        print(f"dashboard: {project}: {report.up} of {len(report.sites)} addresses up{note}")
    makers = {(project, CARD_ID): uptime_maker(report, clock) for project, report in reports.items()}
    return OverlayPortfolio(base, makers)
