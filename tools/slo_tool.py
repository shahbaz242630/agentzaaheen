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
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import httpx
from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import Tool
from agentzaaheen.core.metrics import Counts, Objective, SliKind, SloStatus, hourly_status
from agentzaaheen.core.metrics.revenue import Unknown
from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import Severity, SourceError, UptimeHistory, get_logger, say
from agentzaaheen.plugins.connectors import availability
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

    async def refresh(self) -> None:
        """Read once if the last try was at least :data:`EVERY` ago; a failure keeps the last read."""
        now = self._clock()
        if self._tried_at is not None and now - self._tried_at < EVERY:
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
