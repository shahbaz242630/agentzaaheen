"""Open the dashboard on this laptop (F04): sample data, optionally with real Paddle numbers.

Serves the built web UI and its read-only API on http://127.0.0.1:8765 only.
It refuses any other address: the dashboard has no login yet, so it must
never be reachable from the network.

With ``--paddle-key-env NAME`` it first reads Zaaheen's live Paddle account
once (GET only, through the core's one HTTP client) and shows those numbers on
the Revenue card; every other card stays sample. The key comes from the named
environment variable and is never printed. The numbers are held in memory
only, and turn to "unknown" once they are older than the freshness rule allows
(restart to read again).

With ``--github-token-env NAME`` it also reads each project's GitHub
repository once (GET only: open issues, open pull requests and the newest
build on the main branch, counts only) and shows them on its Code card. The
token comes from the named environment variable and is never printed. A
repository that cannot be read shows why on its card; the others still show.

With ``--model-url URL`` the chat on the right answers for real, from the
cards on screen, using the model server at that address (the local model
trial, F05: ``uv run python -m tools.local_model`` serves it on
http://127.0.0.1:8766). Without it the chat says it is not connected.

Usage:
    cd apps/dashboard && npm ci && npm run build     # once, and after UI changes
    uv run python -m tools.dashboard [--port 8765]
    uv run python -m tools.dashboard --paddle-key-env PADDLE_LIVE_API_KEY_ZAAHEEN
    uv run python -m tools.dashboard --github-token-env GITHUB_READ_TOKEN_YAQZAN
    uv run python -m tools.dashboard --model-url http://127.0.0.1:8766
    uv run python -m tools.dashboard --export-contract   # refresh the UI's contract fixture
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from yaqzan.core.api import LOCAL_HOSTS, ChatBackend, Portfolio, SamplePortfolio, create_app
from yaqzan.core.api.code import CARD_ID as CODE_CARD
from yaqzan.core.api.code import code_card, failed_code_card
from yaqzan.core.api.overlay import CardMaker, OverlayPortfolio
from yaqzan.core.api.revenue import revenue_card
from yaqzan.core.api.views import Card
from yaqzan.core.llm_gateway import FactChat
from yaqzan.core.net import JsonClient, ModelClient, NetError, NetPolicy
from yaqzan.core.scheduler import snapshot_from_rows
from yaqzan.core.sdk import RepoHealth, RevenueBatch, SourceError, explain
from yaqzan.plugins.connectors import github
from yaqzan.plugins.connectors.paddle import ALLOWED_HOSTS, API_VERSION_HEADER, LIVE_BASE_URL, PaddleSource

UI_ROOT = Path(__file__).resolve().parent.parent / "apps" / "dashboard"
UI_BUILD = UI_ROOT / "dist"
CONTRACT_FIXTURE = UI_ROOT / "src" / "contract" / "sample.json"
CONTRACT_TIME = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Read once at start: treated as an hourly source, so it shows "unknown" after two hours.
PADDLE_SCHEDULE = timedelta(hours=1)
# Read once at start, like Paddle: "unknown" after two hours.
GITHUB_SCHEDULE = timedelta(hours=1)
# Which repository each project's Code card reads (checked against the local clones, 20 Oct 2026).
GITHUB_REPOS: dict[str, str] = {
    "zaaheen": "shahbaz242630/zaaheen",
    "sandoq-kin": "shahbaz242630/Document-Vault",
    "agent-x": "shahbaz242630/agent-x",
    "eva": "shahbaz242630/AI-Agent-SAAS",
}
# The model menu entry the local model trial connects (see SamplePortfolio.models).
LOCAL_MODEL_ID = "local-gemma"

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def build_app(
    static_dir: Path = UI_BUILD,
    now: datetime | None = None,
    portfolio: Portfolio | None = None,
    chat: ChatBackend | None = None,
) -> FastAPI:
    """The dashboard app, serving the UI from ``static_dir`` (sample data unless given a portfolio)."""
    chosen = portfolio or SamplePortfolio(now or _now())
    return create_app(chosen, static_dir=static_dir, allowed_hosts=LOCAL_HOSTS, chat=chat)


def model_chat(model_url: str, transport: httpx.AsyncBaseTransport | None = None) -> ChatBackend | None:
    """The chat backed by the model server at ``model_url``, or ``None`` (after saying why) if refused."""
    try:
        client = ModelClient(model_url, transport=transport)
    except NetError as exc:
        print(f"dashboard: the model address was refused ({exc.net_code.value}).")
        print("  Use the local model server, e.g. http://127.0.0.1:8766")
        return None
    return FactChat(client, LOCAL_MODEL_ID)


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


def paddle_card(batch: RevenueBatch, read_at: datetime, clock: Clock = _now) -> CardMaker:
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
    clock: Clock = _now,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Portfolio | None:
    """Sample data with the real Zaaheen Paddle card, or ``None`` (after saying why) if the read failed."""
    raw = environ.get(key_env, "").strip()
    if not raw:
        print(f"dashboard: {key_env} is not set, so Paddle was not read.")
        return None
    if not _usable_secret(raw):
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


def _usable_secret(raw: str) -> bool:
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


def github_card(result: RepoHealth | SourceError, clock: Clock = _now) -> CardMaker:
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
    clock: Clock = _now,
    transport: httpx.AsyncBaseTransport | None = None,
    repos: Mapping[str, str] | None = None,
) -> Portfolio | None:
    """``base`` with each project's real Code card, or ``None`` (after saying why) if there is no token."""
    repos = GITHUB_REPOS if repos is None else repos
    raw = environ.get(token_env, "").strip()
    if not raw:
        print(f"dashboard: {token_env} is not set, so GitHub was not read.")
        return None
    if not _usable_secret(raw):
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


def contract_sample() -> str:
    """The sample API answers as JSON, for the UI's tests to check against its zod shapes."""
    portfolio = SamplePortfolio(CONTRACT_TIME)
    projects = portfolio.projects()
    answers = {
        "projects": [p.model_dump(mode="json") for p in projects],
        "details": {p.id: _detail_json(portfolio, p.id) for p in projects},
        "models": portfolio.models().model_dump(mode="json"),
    }
    return json.dumps(answers, indent=2, ensure_ascii=False) + "\n"


def _detail_json(portfolio: SamplePortfolio, project_id: str) -> object:
    detail = portfolio.project(project_id)
    if detail is None:  # pragma: no cover - ids come from the same portfolio
        raise LookupError(project_id)
    return detail.model_dump(mode="json")


class _NotStartedError(Exception):
    """A requested real-data read failed, after saying why."""


def _real_data(
    ns: argparse.Namespace,
    env: Mapping[str, str],
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[Portfolio | None, list[str]]:
    """The portfolio with every requested real source laid over the sample, and what is real."""
    portfolio: Portfolio | None = None
    live: list[str] = []
    if ns.paddle_key_env:
        portfolio = live_portfolio(ns.paddle_key_env, env, clock=clock, transport=transport)
        if portfolio is None:
            raise _NotStartedError
        live.append("Paddle for Zaaheen")
    if ns.github_token_env:
        base = portfolio or SamplePortfolio(clock())
        portfolio = github_portfolio(base, ns.github_token_env, env, clock=clock, transport=transport)
        if portfolio is None:
            raise _NotStartedError
        live.append("GitHub for every project")
    return portfolio, live


def main(  # noqa: PLR0913  # the real world (files, server, env, network, clock) is injectable for tests
    argv: Sequence[str] | None = None,
    *,
    static_dir: Path = UI_BUILD,
    serve: Callable[[FastAPI, str, int], None] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = _now,
) -> int:
    """Start the dashboard; return 1 if the UI is not built, a Paddle read failed or a token is missing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--export-contract", action="store_true", help="rewrite the UI's contract fixture")
    parser.add_argument("--paddle-key-env", help="env var holding the live READ-ONLY Zaaheen Paddle key")
    parser.add_argument("--github-token-env", help="env var holding the READ-ONLY GitHub token")
    parser.add_argument("--model-url", help="the chat's model server, e.g. http://127.0.0.1:8766")
    ns = parser.parse_args(argv)
    if ns.export_contract:
        CONTRACT_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_FIXTURE.write_text(contract_sample(), encoding="utf-8", newline="\n")
        print(f"dashboard: wrote {CONTRACT_FIXTURE.relative_to(UI_ROOT.parent.parent).as_posix()}")
        return 0
    if not 1024 <= ns.port <= 65535:
        print("dashboard: pick a port between 1024 and 65535.")
        return 1
    if not (static_dir / "index.html").is_file():
        print("dashboard: the web UI is not built yet. Run: cd apps/dashboard && npm ci && npm run build")
        return 1
    env = os.environ if environ is None else environ
    try:
        portfolio, live = _real_data(ns, env, clock, transport)
    except _NotStartedError:
        return 1
    chat: ChatBackend | None = None
    if ns.model_url:
        chat = model_chat(ns.model_url, transport)
        if chat is None:
            return 1
    app = build_app(static_dir, portfolio=portfolio, chat=chat)
    what = f"real numbers from {' and '.join(live)}, sample data elsewhere" if live else "sample data"
    if chat is not None:
        what += "; chat connected to the local model"
    print(f"dashboard: open http://{HOST}:{ns.port} ({what}; press Ctrl+C to stop)")
    (serve or _serve)(app, HOST, ns.port)
    return 0


def _serve(app: FastAPI, host: str, port: int) -> None:  # pragma: no cover - starts a real server
    uvicorn.run(app, host=host, port=port, log_level="warning", server_header=False)


if __name__ == "__main__":
    sys.exit(main())
