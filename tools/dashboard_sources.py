"""Where the dashboard's real numbers come from on this laptop: Paddle and GitHub, read once at start.

Split from ``tools.dashboard`` (which re-exports these names). Every read is
GET only, through the core's one HTTP client; keys come from environment
variables and are never printed. A failed read says what, why and what to do.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from agentzaaheen.core.api import Portfolio, SamplePortfolio
from agentzaaheen.core.api.code import CARD_ID as CODE_CARD
from agentzaaheen.core.api.code import code_card, failed_code_card
from agentzaaheen.core.api.overlay import CardMaker, OverlayPortfolio
from agentzaaheen.core.api.revenue import revenue_card
from agentzaaheen.core.api.views import Card
from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.scheduler import snapshot_from_rows
from agentzaaheen.core.sdk import RepoHealth, RevenueBatch, SourceError, explain
from agentzaaheen.plugins.connectors import github
from agentzaaheen.plugins.connectors.paddle import (
    ALLOWED_HOSTS,
    API_VERSION_HEADER,
    LIVE_BASE_URL,
    PaddleSource,
)
from tools.laptop_db import LAPTOP_REPOS

# Read once at start: treated as an hourly source, so it shows "unknown" after two hours.
PADDLE_SCHEDULE = timedelta(hours=1)
# Read once at start, like Paddle: "unknown" after two hours.
GITHUB_SCHEDULE = timedelta(hours=1)
# Which repository each project's Code card reads (the laptop's five projects).
GITHUB_REPOS: dict[str, str] = dict(LAPTOP_REPOS)

Clock = Callable[[], datetime]


class ListNotUsableError(Exception):
    """A local list given at start (uptime, Supabase) could not be used; why was already said."""


def utc_now() -> datetime:
    """The time now, in UTC."""
    return datetime.now(UTC)


def month_start(moment: datetime) -> datetime:
    """Midnight UTC on the first day of the month that ``moment`` falls in."""
    return moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def read_paddle(
    key: SecretStr, now: datetime, transport: httpx.AsyncBaseTransport | None = None
) -> RevenueBatch:
    """One read-only fetch from the live Paddle account, payments from the start of this month."""
    async with JsonClient(
        LIVE_BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
        headers=API_VERSION_HEADER,
        secret_headers={"Authorization": SecretStr(f"Bearer {key.get_secret_value()}")},
        transport=transport,
    ) as http:
        return await PaddleSource(http).fetch(payments_since=month_start(now), now=now)


def paddle_card(batch: RevenueBatch, read_at: datetime, clock: Clock = utc_now) -> CardMaker:
    """Builds the Zaaheen Revenue card from ``batch``, judging freshness each time it is shown."""

    def make() -> Card:
        now = clock()
        snapshot = snapshot_from_rows(
            "paddle",
            batch.subscriptions,
            batch.payments,
            batch.refunds,
            now=now,
            last_success=read_at,
            schedule=PADDLE_SCHEDULE,
            start=month_start(read_at),
            end=now,
        )
        return revenue_card(
            snapshot,
            card_id="paddle",
            title="Revenue",
            source_label="Paddle (live)",
            home_currency="USD",
            period_label="This month",
        )

    return make


def live_portfolio(
    key_env: str,
    environ: Mapping[str, str],
    *,
    clock: Clock = utc_now,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Portfolio | None:
    """Sample data with the real Zaaheen Paddle card, or ``None`` (after saying why) if the read failed."""
    raw = environ.get(key_env, "").strip()
    if not raw:
        print(f"dashboard: {key_env} is not set, so Paddle was not read.")
        return None
    if not usable_secret(raw):
        print("dashboard: the key contains spaces or control characters; copy it again.")
        return None
    read_at = clock()
    print("dashboard: reading the Zaaheen Paddle account (live, read-only)...")
    try:
        batch = asyncio.run(read_paddle(SecretStr(raw), read_at, transport))
    except SourceError as exc:
        code = exc.detail_code or exc.code
        said = explain(code, vendor="Paddle")
        where = f" at {exc.operation}" if exc.operation else ""
        print(f"dashboard: the Paddle read failed with {code}{where}.")
        print(f"  What: {said.what}")
        print(f"  Why: {said.why}")
        print(f"  What to do: {said.fix}")
        return None
    print(
        f"dashboard: read {len(batch.subscriptions)} subscriptions, {len(batch.payments)} payment attempts "
        f"and {len(batch.refunds)} refunds."
    )
    makers = {("zaaheen", "paddle"): paddle_card(batch, read_at, clock)}
    return OverlayPortfolio(SamplePortfolio(read_at), makers)


def usable_secret(raw: str) -> bool:
    """A pasted key or token that has no spaces or control characters."""
    return raw.isascii() and raw.isprintable() and " " not in raw


async def read_github(
    token: SecretStr,
    repos: Mapping[str, str],
    now: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, RepoHealth | SourceError]:
    """One read-only look at each repository, keyed by project; a failure is kept, not raised."""
    async with JsonClient(
        github.BASE_URL,
        policy=NetPolicy(allowed_hosts=github.ALLOWED_HOSTS),
        headers=github.API_HEADERS,
        secret_headers={"Authorization": SecretStr(f"Bearer {token.get_secret_value()}")},
        transport=transport,
    ) as http:
        out: dict[str, RepoHealth | SourceError] = {}
        for project, repo in repos.items():  # one at a time: GitHub limits searches per minute
            try:
                out[project] = await github.GitHubSource(http, repo).fetch(now=now)
            except SourceError as exc:
                out[project] = exc
        return out


def github_card(result: RepoHealth | SourceError, clock: Clock = utc_now) -> CardMaker:
    """Builds one project's Code card, judging freshness each time it is shown."""

    def make() -> Card:
        if isinstance(result, SourceError):
            return failed_code_card(result.reason_code, source_label="GitHub (live)")
        return code_card(result, source_label="GitHub (live)", schedule=GITHUB_SCHEDULE, now=clock())

    return make


def github_portfolio(  # noqa: PLR0913  # the base, the token's name, and the injectable outside world
    base: Portfolio,
    token_env: str,
    environ: Mapping[str, str],
    *,
    clock: Clock = utc_now,
    transport: httpx.AsyncBaseTransport | None = None,
    repos: Mapping[str, str] | None = None,
) -> Portfolio | None:
    """``base`` with each project's real Code card, or ``None`` (after saying why) if there is no token."""
    repos = GITHUB_REPOS if repos is None else repos
    raw = environ.get(token_env, "").strip()
    if not raw:
        print(f"dashboard: {token_env} is not set, so GitHub was not read.")
        return None
    if not usable_secret(raw):
        print("dashboard: the token contains spaces or control characters; copy it again.")
        return None
    print(f"dashboard: reading {len(repos)} GitHub repositories (read-only)...")
    results = asyncio.run(read_github(SecretStr(raw), repos, clock(), transport))
    for project, result in results.items():
        if isinstance(result, SourceError):
            said = explain(result.reason_code, vendor="GitHub")
            where = f" at {result.operation}" if result.operation else ""
            print(f"dashboard: {repos[project]} could not be read ({result.reason_code}{where}).")
            print(f"  What: {said.what}")
            print(f"  What to do: {said.fix}")
        else:
            print(
                f"dashboard: {repos[project]}: {result.open_issues} open issues, "
                f"{result.open_pull_requests} open pull requests, build {result.build.value}."
            )
    makers = {(project, CODE_CARD): github_card(result, clock) for project, result in results.items()}
    return OverlayPortfolio(base, makers)
