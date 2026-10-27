"""The chat's ``slo_status`` tool: how one project stands against its SLOs, computed by code (ADR-032).

The objectives come from the local SLO list (``projects/slo.yaml``,
``tools.slo_list``). Availability is worked out from the external checker's
hourly counts (connector ``availability``), read with a read-only Cloudflare
key at most once an hour (:class:`AvailabilityReader`); a failed read keeps
the last one, which turns "unknown" once it is older than :data:`FRESH`.
Speed and error objectives say "not connected yet": the products do not send
request timings or errors yet (08-OBSERVABILITY build order step 4).

Every number in the text is computed in ``core.metrics.slo``; the text only
words it. The tool is offered while the ``check-slo`` skill and the
``availability`` connector are both on (the registry switches, ADR-030).

With the connector on, :func:`slo_portfolio` also reads the counts at start
and every hour, and lays each project's "Uptime target" card
(``core.api.slo``) over its sample placeholder, so the page and the chat's
facts show the same numbers.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import httpx
from pydantic import SecretStr

from agentzaaheen.core.api import Portfolio
from agentzaaheen.core.api.overlay import CardMaker, OverlayPortfolio
from agentzaaheen.core.api.slo import CARD_ID, slo_card
from agentzaaheen.core.api.views import Card
from agentzaaheen.core.llm_gateway import Tool
from agentzaaheen.core.metrics import Counts, Objective, SliKind, SloStatus, hourly_status
from agentzaaheen.core.metrics.revenue import Unknown
from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import Severity, SourceError, TargetStanding, UptimeHistory, get_logger, say
from agentzaaheen.plugins.connectors import availability
from tools.dashboard_reread import Reread
from tools.dashboard_sources import ListNotUsableError
from tools.slo_list import SloListError, load_slos

# The registry ids this tool belongs to (the skill's folder, the connector's folder).
CHECK_SLO_SKILL = "check-slo"
AVAILABILITY_CONNECTOR = "availability"
EVERY = timedelta(hours=1)  # read at most this often
FRESH = timedelta(hours=2)  # older counts are not used: unknown, never fine
CHECKER_DB = re.compile(r"^(?P<account>[0-9a-f]{32})/(?P<database>[0-9a-f-]{36})$")

Clock = Callable[[], datetime]


class AvailabilityReader:
    """Reads the checker's hourly counts when asked and due; :attr:`latest` is the newest good read."""

    def __init__(  # noqa: PLR0913  # the key, where the counts live, the time, and the network
        self,
        key: SecretStr,
        *,
        account: str,
        database: str,
        clock: Clock,
        window: timedelta,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = SecretStr(f"Bearer {key.get_secret_value()}")
        self._path = availability.query_path(account, database)  # refuses ids that are not Cloudflare's
        self._account, self._database = account, database
        self._clock = clock
        self._window = window
        self._transport = transport
        self._tried_at: datetime | None = None
        self._log = get_logger("agentzaaheen.availability")
        self.latest: UptimeHistory | None = None

    async def refresh(self, *, force: bool = False) -> None:
        """Read once if due (an hour since the last try) or ``force``d; a failure keeps the last read."""
        now = self._clock()
        if not force and self._tried_at is not None and now - self._tried_at < EVERY:
            return
        self._tried_at = now
        try:
            async with JsonClient(
                availability.BASE_URL,
                policy=NetPolicy(allowed_hosts=availability.ALLOWED_HOSTS),
                headers=availability.API_HEADERS,
                secret_headers={availability.KEY_HEADER: self._key},
                transport=self._transport,
                read_queries=availability.READ_QUERIES,
            ) as http:
                source = availability.AvailabilitySource(http, account=self._account, database=self._database)
                history = await source.fetch(since=now - self._window, now=now)
        except SourceError as exc:
            say(self._log, "availability_read_failed", code=exc.code.value)
            return
        self.latest = history
        say(self._log, "availability_read_done", count=len(history.hours))


def availability_reader(  # noqa: PLR0913  # the env, which key, where the counts live, the time, the network
    env: Mapping[str, str],
    key_env: str | None,
    checker_db: str | None,
    *,
    clock: Clock,
    window: timedelta,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AvailabilityReader | None:
    """The reader, if ``checker_db`` (``ACCOUNT/DATABASE``) and ``key_env`` are usable (said on screen)."""
    if not key_env or not checker_db:
        return None
    match = CHECKER_DB.fullmatch(checker_db)
    if match is None:
        print("dashboard: --checker-db must be ACCOUNT_ID/DATABASE_ID (Cloudflare's hex ids).")
        return None
    raw = env.get(key_env, "").strip()
    if not raw or not raw.isascii() or not raw.isprintable() or " " in raw:
        print(f"dashboard: {key_env} is not set (or not usable), so the SLO numbers say 'not connected'.")
        return None
    try:
        reader = AvailabilityReader(
            SecretStr(raw),
            account=match["account"],
            database=match["database"],
            clock=clock,
            window=window,
            transport=transport,
        )
    except ValueError:
        print("dashboard: --checker-db must be ACCOUNT_ID/DATABASE_ID (Cloudflare's hex ids).")
        return None
    print(
        "dashboard: the SLO numbers read the checker's uptime counts (read-only key), at most once an hour."
    )
    return reader


@dataclass(frozen=True)
class SloBook:
    """The SLO list, and the reader of the checker's counts (``None``: not connected)."""

    objectives: tuple[Objective, ...]
    reader: AvailabilityReader | None


def slo_book(  # noqa: PLR0913  # the list, what exists, the key, where the counts live, time, network
    path: Path | None,
    projects: Collection[str],
    env: Mapping[str, str],
    key_env: str | None,
    checker_db: str | None,
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SloBook | None:
    """The SLO list given with ``--slo`` (``None`` without one); a broken list is said and raised."""
    if path is None:
        return None
    log = get_logger("agentzaaheen.settings")
    try:
        objectives = load_slos(path, projects)
    except SloListError as exc:
        print(f"dashboard: slo: {exc}.")  # the exact rule, on the console only: it can quote the file
        say(log, "settings_refused", setting="SLO")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="SLO", entries=len(objectives))
    window = max(o.window for o in objectives)
    reader = availability_reader(env, key_env, checker_db, clock=clock, window=window, transport=transport)
    return SloBook(objectives, reader)


def statuses(
    objectives: Sequence[Objective], history: UptimeHistory | None, now: datetime
) -> list[tuple[Objective, SloStatus | None, str]]:
    """Each objective with its status, or ``None`` and why it cannot be given."""
    out: list[tuple[Objective, SloStatus | None, str]] = []
    for objective in objectives:
        if objective.kind is not SliKind.AVAILABILITY:
            out.append(
                (objective, None, "not connected yet: the product does not send request timings or errors")
            )
        elif history is None:
            out.append((objective, None, "not connected yet: the checker's counts have not been read"))
        elif now - history.fetched_at > FRESH:
            out.append((objective, None, f"unknown: the newest counts are from {_at(history.fetched_at)}"))
        else:
            hours = [
                (h.hour, Counts(h.good, h.total)) for h in history.hours if h.project == objective.project
            ]
            out.append((objective, hourly_status(objective, hours, now), ""))
    return out


def slo_text(
    project: str, objectives: Sequence[Objective], history: UptimeHistory | None, now: datetime
) -> str:
    """The project's objectives as the model reads them, every number computed by code."""
    mine = [o for o in objectives if o.project == project]
    head = f"Project: {project}. Service level objectives (SLOs); every number below is computed by code."
    if not mine:
        return f"{head}\nThis project has no SLOs."
    lines = [head]
    for objective, status, why in statuses(mine, history, now):
        lines.append(_line(objective, status, why))
    return "\n".join(lines)


def _line(objective: Objective, status: SloStatus | None, why: str) -> str:
    name = f"- {objective.name}: target {_pct(objective.target)} over {objective.window.days} days"
    if status is None:
        return f"{name}. {why.capitalize()}."
    if isinstance(status.compliance, Unknown) or isinstance(status.budget_left, Unknown):
        return f"{name}. Unknown: no checks were counted in the window."
    days = status.measured.total_seconds() / 86400
    short = objective.window - status.measured >= timedelta(hours=1)  # counted by whole hours
    measured = f" (measured over {days:.1f} days so far)" if short else ""
    within = "within target" if status.compliance >= objective.target else "BELOW target"
    parts = [
        f"{name}{measured}.",
        f"{_pct(status.compliance)} of {status.counts.total:,} checks found it up: {within}.",
        _budget(status, status.budget_left),
        _burn(status.burn),
    ]
    return " ".join(parts)


def _budget(status: SloStatus, left: Fraction) -> str:
    allowed = _duration(status.objective.allowed_downtime)
    if left <= 0:
        times = float(1 - left)  # the burn rate: failures as a multiple of what the target allows
        return f"Error budget: spent; failures ran at {times:.1f} times what the target allows ({allowed})."
    time_left = status.time_left
    spare = "" if time_left is None else f", about {_duration(time_left)} of {allowed}"
    return f"Error budget left: {_pct(left)}{spare}."


def _burn(burn: Severity | None) -> str:
    if burn is Severity.P1:
        return "Burn alert P1: spending the budget fast right now."
    if burn is Severity.P2:
        return "Burn alert P2: a slow burn over the last 3 days."
    return "No burn alert from the hourly counts."


def _pct(value: Fraction) -> str:
    return f"{float(value) * 100:.2f}%"


def _duration(span: timedelta) -> str:
    minutes = int(span.total_seconds()) // 60
    return f"{minutes // 60} h {minutes % 60} min"


def _at(when: datetime) -> str:
    return f"{when:%d %b %H:%M} UTC"


def slo_status_tool(
    objectives: Sequence[Objective], reader: AvailabilityReader | None, clock: Clock
) -> Tool | None:
    """The ``slo_status`` tool for the projects that have objectives (none: ``None``)."""
    choices = tuple(sorted({o.project for o in objectives}))
    if not choices:
        return None

    async def run(project: str) -> str:
        if reader is not None:
            await reader.refresh()
        return slo_text(project, objectives, None if reader is None else reader.latest, clock())

    return Tool(
        name="slo_status",
        description="How one project stands against its SLOs: within target, error budget left, burn alerts.",
        choices=choices,
        run=run,
        trusted=True,
        label="SLOs",
        workspace_of=lambda project: project,
    )


def slo_portfolio(
    base: Portfolio, book: SloBook, reader: AvailabilityReader, *, clock: Clock, reads: list[Reread]
) -> Portfolio:
    """``base`` with each project's real "Uptime target" card, read now and again every hour."""
    asyncio.run(reader.refresh(force=True))

    async def again() -> int:
        await reader.refresh(force=True)
        return 0  # one read for every project: a failure is logged by the reader and keeps the last read

    reads.append(Reread(source=AVAILABILITY_CONNECTOR, read=again))
    makers: dict[tuple[str, str], CardMaker] = {}
    for objective in book.objectives:
        detail = base.project(objective.project)
        has_place = detail is not None and any(card.id == CARD_ID for card in detail.cards)
        if objective.kind is SliKind.AVAILABILITY and has_place:
            makers[(objective.project, CARD_ID)] = _card_maker(objective, reader, clock)
    print(f"dashboard: {len(makers)} projects show their uptime target from the checker's counts.")
    return OverlayPortfolio(base, makers)


def _card_maker(objective: Objective, reader: AvailabilityReader, clock: Clock) -> CardMaker:
    def make() -> Card:
        history = reader.latest
        now = clock()
        if history is None:
            return slo_card(objective, None, read_at=None, schedule=EVERY, now=now)
        hours = [(h.hour, Counts(h.good, h.total)) for h in history.hours if h.project == objective.project]
        status = hourly_status(objective, hours, now)
        return slo_card(objective, status, read_at=history.fetched_at, schedule=EVERY, now=now)

    return make


class TargetStandings:
    """Each project's standing against its uptime target, for the Monitoring agents (``targets.read``)."""

    def __init__(self, book: SloBook, reader: AvailabilityReader, *, clock: Clock) -> None:
        self._objectives = {o.project: o for o in book.objectives if o.kind is SliKind.AVAILABILITY}
        self._reader = reader
        self._clock = clock
        self.listed = frozenset(self._objectives)

    async def refresh(self) -> None:
        """Read the checker's counts if due (once an hour); a failure keeps the last read."""
        await self._reader.refresh()

    def standing(self, project: str) -> TargetStanding | None:
        """How ``project`` stands now, every number worked out in ``core.metrics`` (``None``: not read)."""
        objective = self._objectives.get(project)
        history = self._reader.latest
        if objective is None or history is None:
            return None
        hours = [(h.hour, Counts(h.good, h.total)) for h in history.hours if h.project == project]
        status = hourly_status(objective, hours, self._clock())
        up, left = status.compliance, status.budget_left
        return TargetStanding(
            objective=objective.name,
            target_bp=_bp(objective.target),
            up_bp=None if isinstance(up, Unknown) else _bp(up),
            budget_left_bp=None if isinstance(left, Unknown) else max(-1_000_000, _bp(left)),
            burning=status.burn is not None,
            checks=status.counts.total,
            read_at=history.fetched_at,
        )


def _bp(share: Fraction) -> int:
    """A share in basis points, rounded down: never better than it was."""
    return math.floor(share * 10_000)
