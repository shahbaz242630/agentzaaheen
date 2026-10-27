"""Supabase on this laptop: read the local list, read each project hourly, show Backend cards (F03 slice 6).

The list lives in ``projects/supabase.yaml`` (git-ignored and path-guarded,
Rulebook §7b). Each project names its Supabase project ref and the
environment variable that holds the scoped, read-only token for its
organization (one token per organization)::

    sandoq-kin: {ref: abcdefghijklmnopqrst, token_env: SUPABASE_READ_TOKEN_SANDOQ}
    zaaheen: {ref: ..., token_env: ..., unused: [rest]}   # a service switched off on purpose

Tokens come from environment variables only and are never printed. A project
whose token is missing or refused shows why on its card; the others still show.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agentzaaheen.core.api import Portfolio
from agentzaaheen.core.api.backend import CARD_ID, backend_card, failed_backend_card
from agentzaaheen.core.api.overlay import CardMaker, OverlayPortfolio
from agentzaaheen.core.api.views import Card
from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import (
    PROJECT_REF,
    BackendHealth,
    SourceError,
    SourceErrorCode,
    explain,
    get_logger,
    say,
)
from agentzaaheen.plugins.connectors import supabase
from tools.dashboard_reread import Latest, Reread
from tools.dashboard_sources import ListNotUsableError

# Read at start, then again every hour (``tools.dashboard_reread``): "unknown" after two hours without a read.
SUPABASE_SCHEDULE = timedelta(hours=1)
MAX_LIST_BYTES = 16 * 1024
SOURCE_LABEL = "Supabase (live)"

Clock = Callable[[], datetime]


class SupabaseListError(Exception):
    """The project list could not be used; the message says why in plain words."""


class SupabaseProject(BaseModel):
    """One project on the list: its Supabase ref and where its organization's token is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(pattern=PROJECT_REF)
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    # Services the project switched off on purpose (e.g. Zaaheen's Data API, "rest"): never asked about.
    unused: tuple[Literal["db", "auth", "rest", "storage", "realtime"], ...] = ()


def load_supabase(path: Path, projects: Collection[str]) -> dict[str, SupabaseProject]:
    """Each project's Supabase ref and token variable from ``path``; only known projects are allowed."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise SupabaseListError(f"the Supabase list {path} could not be read") from None
    if len(raw) > MAX_LIST_BYTES:
        raise SupabaseListError("the Supabase list is too big")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise SupabaseListError("the Supabase list is not valid YAML") from None
    if not isinstance(data, dict) or not data:
        raise SupabaseListError("the Supabase list must map each project to its ref and token variable")
    out: dict[str, SupabaseProject] = {}
    for project, entry in data.items():
        if project not in projects:
            raise SupabaseListError(f"the Supabase list names an unknown project: {project!r}")
        try:
            out[project] = SupabaseProject.model_validate(entry)
        except ValidationError as exc:
            raise SupabaseListError(f"{project}: not usable ({exc.errors()[0]['msg']})") from None
    return out


@dataclass(frozen=True)
class _Missing:
    token_env: str


async def read_supabase(
    listed: Mapping[str, SupabaseProject],
    env: Mapping[str, str],
    now: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, BackendHealth | SourceError | _Missing]:
    """Each listed project read once with its own organization's token; a failure is kept, not raised."""
    out: dict[str, BackendHealth | SourceError | _Missing] = {}
    for project, entry in listed.items():
        raw = env.get(entry.token_env, "").strip()
        if not raw or not raw.isascii() or not raw.isprintable() or " " in raw:
            out[project] = _Missing(entry.token_env)
            continue
        async with JsonClient(
            supabase.BASE_URL,
            policy=NetPolicy(allowed_hosts=supabase.ALLOWED_HOSTS),
            headers=supabase.API_HEADERS,
            secret_headers={"Authorization": SecretStr(f"Bearer {raw}")},
            transport=transport,
        ) as http:
            try:
                source = supabase.SupabaseSource(http, entry.ref, unused=entry.unused)
                out[project] = await source.fetch(now=now)
            except SourceError as exc:
                out[project] = exc
    return out


def supabase_maker(result: BackendHealth | SourceError | _Missing, clock: Clock) -> CardMaker:
    """Builds one project's Backend card, judging freshness each time it is shown."""

    def make() -> Card:
        if isinstance(result, _Missing):
            return failed_backend_card(SourceErrorCode.AUTH.value, source_label=SOURCE_LABEL)
        if isinstance(result, SourceError):
            return failed_backend_card(result.reason_code, source_label=SOURCE_LABEL)
        return backend_card(result, source_label=SOURCE_LABEL, schedule=SUPABASE_SCHEDULE, now=clock())

    return make


def supabase_portfolio(  # noqa: PLR0913  # the base, the list, and the injectable outside world
    base: Portfolio,
    listed: Mapping[str, SupabaseProject],
    env: Mapping[str, str],
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
    reads: list[Reread] | None = None,
) -> Portfolio:
    """``base`` with each listed project's real Backend card, after saying how each one stood.

    Given ``reads``, its hourly re-read is added there.
    """
    print(f"dashboard: reading {len(listed)} Supabase projects (read-only)...")
    results = asyncio.run(read_supabase(listed, env, clock(), transport))
    for project, result in results.items():
        if isinstance(result, _Missing):
            print(
                f"dashboard: {project}: {result.token_env} is not set, so its Supabase project was not read."
            )
        elif isinstance(result, SourceError):
            said = explain(result.reason_code, vendor="Supabase")
            print(f"dashboard: {project}: Supabase could not be read ({result.reason_code}). {said.fix}")
        else:
            down = len(result.services) - result.healthy_services
            print(f"dashboard: {project}: Supabase {result.state.value}, {down} unhealthy services.")
    latest = Latest(results)
    if reads is not None:

        async def reread() -> int:
            latest.value = await read_supabase(listed, env, clock(), transport)
            return sum(1 for result in latest.value.values() if not isinstance(result, BackendHealth))

        reads.append(Reread("Supabase", reread))
    makers = {(project, CARD_ID): _latest_backend(latest, project, clock) for project in results}
    return OverlayPortfolio(base, makers)


def _latest_backend(
    latest: Latest[dict[str, BackendHealth | SourceError | _Missing]], project: str, clock: Clock
) -> CardMaker:
    def newest() -> Card:  # built from ``latest`` on every request, never from the read at start
        return supabase_maker(latest.value[project], clock)()

    return newest


def supabase_list(path: Path | None, projects: Collection[str]) -> dict[str, SupabaseProject]:
    """The local list given with ``--supabase`` (empty without one); a broken list is said and raised."""
    if path is None:
        return {}
    log = get_logger("agentzaaheen.settings")
    try:
        found = load_supabase(path, projects)
    except SupabaseListError as exc:
        print(f"dashboard: {exc}.")  # the exact rule, on the console only: it can quote the file
        say(log, "settings_refused", setting="Supabase")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="Supabase", entries=len(found))
    return found
