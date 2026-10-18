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

Usage:
    cd apps/dashboard && npm ci && npm run build     # once, and after UI changes
    uv run python -m tools.dashboard [--port 8765]
    uv run python -m tools.dashboard --paddle-key-env PADDLE_LIVE_API_KEY_ZAAHEEN
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

from yaqzan.core.api import LOCAL_HOSTS, Portfolio, SamplePortfolio, create_app
from yaqzan.core.api.overlay import CardMaker, OverlayPortfolio
from yaqzan.core.api.revenue import revenue_card
from yaqzan.core.api.views import Card
from yaqzan.core.net import JsonClient, NetPolicy
from yaqzan.core.scheduler import snapshot_from_rows
from yaqzan.core.sdk import RevenueBatch, SourceError, explain
from yaqzan.plugins.connectors.paddle import ALLOWED_HOSTS, API_VERSION_HEADER, LIVE_BASE_URL, PaddleSource

UI_ROOT = Path(__file__).resolve().parent.parent / "apps" / "dashboard"
UI_BUILD = UI_ROOT / "dist"
CONTRACT_FIXTURE = UI_ROOT / "src" / "contract" / "sample.json"
CONTRACT_TIME = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Read once at start: treated as an hourly source, so it shows "unknown" after two hours.
PADDLE_SCHEDULE = timedelta(hours=1)

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def build_app(
    static_dir: Path = UI_BUILD, now: datetime | None = None, portfolio: Portfolio | None = None
) -> FastAPI:
    """The dashboard app, serving the UI from ``static_dir`` (sample data unless given a portfolio)."""
    chosen = portfolio or SamplePortfolio(now or _now())
    return create_app(chosen, static_dir=static_dir, allowed_hosts=LOCAL_HOSTS)


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
    if not raw.isascii() or not raw.isprintable() or " " in raw:
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


def main(  # noqa: PLR0913  # the real world (files, server, env, network, clock) is injectable for tests
    argv: Sequence[str] | None = None,
    *,
    static_dir: Path = UI_BUILD,
    serve: Callable[[FastAPI, str, int], None] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = _now,
) -> int:
    """Start the dashboard; return 1 if the web UI is not built or a requested Paddle read failed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--export-contract", action="store_true", help="rewrite the UI's contract fixture")
    parser.add_argument("--paddle-key-env", help="env var holding the live READ-ONLY Zaaheen Paddle key")
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
    portfolio: Portfolio | None = None
    if ns.paddle_key_env:
        env = os.environ if environ is None else environ
        portfolio = live_portfolio(ns.paddle_key_env, env, clock=clock, transport=transport)
        if portfolio is None:
            return 1
    app = build_app(static_dir, portfolio=portfolio)
    what = "real Paddle numbers for Zaaheen, sample data elsewhere" if portfolio else "sample data"
    print(f"dashboard: open http://{HOST}:{ns.port} ({what}; press Ctrl+C to stop)")
    (serve or _serve)(app, HOST, ns.port)
    return 0


def _serve(app: FastAPI, host: str, port: int) -> None:  # pragma: no cover - starts a real server
    uvicorn.run(app, host=host, port=port, log_level="warning", server_header=False)


if __name__ == "__main__":
    sys.exit(main())
