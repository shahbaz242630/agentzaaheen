"""Uptime on this laptop: read the local address list, check each address once, show the cards (F03 slice 4).

The list lives in ``projects/uptime.yaml``, which is git-ignored and blocked
by the path guard (real project configuration never reaches the public repo,
Rulebook §7b). Each project maps to its addresses, each with a rule::

    zaaheen:
      - {url: "https://example.com", expect: ok}       # a page or a redirect
      - {url: "https://api.example.com", expect: answer}  # anything but a server error
      - {url: "https://api.example.com/v1/x", expect: status, status: 405}  # exactly this code

Every check is a status-only GET through the core's ``StatusProbe``: the page
is never read, redirects are not followed, and only the listed hosts may be
reached. :class:`UptimeLive` re-checks every 5 minutes while the dashboard
runs and, given an ntfy topic, sends phone alerts (ADR-031).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import SecretStr, ValidationError

from tools.dashboard_sources import ListNotUsableError
from yaqzan.core.api import Portfolio
from yaqzan.core.api.overlay import CardMaker, OverlayPortfolio
from yaqzan.core.api.uptime import CARD_ID, uptime_card
from yaqzan.core.api.views import Card
from yaqzan.core.net import PushClient, StatusProbe
from yaqzan.core.notify import Alert, UptimeWatch, deliver
from yaqzan.core.sdk import SiteRule, UptimeReport, get_logger, say, span
from yaqzan.plugins.connectors.uptime import UptimeSource

# Re-checked every 5 minutes while the dashboard runs (ADR-031); "unknown" if no check for two hours.
UPTIME_SCHEDULE = timedelta(hours=1)
RECHECK = timedelta(minutes=5)
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


class UptimeLive:
    """The latest check of every listed project, re-checked every ``interval_s`` while the dashboard runs.

    The first round runs at start (so the cards are real from the first page).
    Then a background thread re-checks; each round's alerts go to ntfy when a
    topic was given (ADR-031). A round that fails is logged and the cards keep
    their last check, which turns "unknown" by its freshness rule.
    """

    def __init__(  # noqa: PLR0913  # the list, the names, the clock, and the injectable outside world
        self,
        rules: Mapping[str, tuple[SiteRule, ...]],
        names: Mapping[str, str],
        *,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        topic: SecretStr | None = None,
        interval_s: float = RECHECK.total_seconds(),
    ) -> None:
        self._rules = dict(rules)
        self._clock = clock
        self._transport = transport
        self._topic = topic
        self._interval_s = interval_s
        self._watch = UptimeWatch(names)
        self._stop = threading.Event()
        self._log = get_logger("yaqzan.uptime")
        self.latest: dict[str, UptimeReport] = {}

    async def round(self) -> list[Alert]:
        """Check every address once, keep the result, and send any alerts it calls for."""
        with span("uptime round") as current:  # one trace per round: every probe and alert inside it
            reports = await read_uptime(self._rules, self._clock(), self._transport)
            self.latest = {**self.latest, **reports}
            alerts = self._watch.observe(reports)
            current.set_attribute("yaqzan.count", len(alerts))
            if alerts and self._topic is not None:
                async with PushClient(self._topic, transport=self._transport) as pusher:
                    await deliver(alerts, pusher)
            sites = [site for report in reports.values() for site in report.sites]
            failing = sum(1 for site in sites if not site.up)
            say(self._log, "uptime_round_done", checked=len(sites), failing=failing, changes=len(alerts))
        for alert in alerts:
            print(f"dashboard: {alert.title}: {alert.message}")
        return alerts

    def start(self) -> threading.Thread:
        """Re-check in the background until :meth:`stop` (a daemon thread: it ends with the dashboard)."""
        thread = threading.Thread(target=self._run, name="yaqzan-uptime", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Stop re-checking after the current wait."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                asyncio.run(self.round())
            except Exception as exc:  # noqa: BLE001  # a failed round must never end the re-checking
                say(self._log, "uptime_round_failed", kind=type(exc).__name__)


def uptime_maker(live: UptimeLive, project: str, clock: Clock) -> CardMaker:
    """Builds one project's Uptime card from its latest check, judging freshness each time it is shown."""

    def make() -> Card:
        return uptime_card(live.latest[project], schedule=UPTIME_SCHEDULE, now=clock())

    return make


def uptime_portfolio(base: Portfolio, live: UptimeLive, *, clock: Clock) -> Portfolio:
    """``base`` with each listed project's real Uptime card, after the first round (saying how each stood)."""
    count = sum(len(report.sites) for report in live.latest.values())
    print(f"dashboard: checked {count} addresses for uptime (status only); re-checking every 5 minutes.")
    for project, report in live.latest.items():
        down = [site.url for site in report.sites if not site.up]
        note = f"; down: {', '.join(down)}" if down else ""
        print(f"dashboard: {project}: {report.up} of {len(report.sites)} addresses up{note}")
    makers = {(project, CARD_ID): uptime_maker(live, project, clock) for project in live.latest}
    return OverlayPortfolio(base, makers)


def start_watch(  # noqa: PLR0913  # the list, the names, where the topic is, and the outside world
    rules: Mapping[str, tuple[SiteRule, ...]],
    names: Mapping[str, str],
    env: Mapping[str, str],
    topic_env: str | None,
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> UptimeLive | None:
    """The watcher after its first round; with ``topic_env``, alerts go to the phone (None if unusable)."""
    topic: SecretStr | None = None
    if topic_env:
        raw = env.get(topic_env, "").strip()
        if not raw or not raw.isascii() or not raw.isprintable() or " " in raw:
            print(f"dashboard: {topic_env} is not set (or not usable), so no alerts can be sent.")
            return None
        topic = SecretStr(raw)
    watcher = UptimeLive(rules, names, clock=clock, transport=transport, topic=topic)
    asyncio.run(watcher.round())
    if topic is not None:
        print("dashboard: uptime alerts go to your phone via ntfy (after 2 failed checks in a row).")
    return watcher


def uptime_list(path: Path | None, projects: Collection[str]) -> dict[str, tuple[SiteRule, ...]]:
    """The local uptime list given with ``--uptime`` (empty without one); a broken list is said and raised."""
    if path is None:
        return {}
    log = get_logger("yaqzan.settings")
    try:
        found = load_uptime(path, projects)
    except UptimeListError as exc:
        print(f"dashboard: {exc}.")  # the exact rule, on the console only: it can quote the file
        say(log, "settings_refused", setting="uptime")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="uptime", entries=len(found))
    return found
