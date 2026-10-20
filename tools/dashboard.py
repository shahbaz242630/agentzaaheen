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
http://127.0.0.1:8766). Without it the chat says it is not connected. The
connected chat may open skills and, with ``--github-token-env``, read a
failed build's log (``tools.chat_tools``, ADR-029).

The Customize page lists every connector, skill and plugin found on disk
(read once at start). ``--bundles DIR`` also shows the plugin bundles under
DIR (Claude, Codex or Cursor layout), view only. With ``--switches`` the
page's on/off switches work and are remembered in the laptop database
(``tools.laptop_db up`` first, ADR-030): a connector that is off is not read,
and the chat may use only what is on. Requests are approved with a Windows
Hello passkey (ADR-SEC-007); passkeys need a name, not an IP, so the
dashboard is opened at http://localhost (it still listens on 127.0.0.1 only).
``--enrol-passkey`` allows the one-time passkey set-up and prints the code
the page asks for. ``--uptime FILE`` checks each project's public addresses
once (status only, never the page) from the local list ``projects/uptime.yaml``
(``tools.dashboard_uptime``) and shows them on each project's Uptime card.

Usage:
    cd apps/dashboard && npm ci && npm run build     # once, and after UI changes
    uv run python -m tools.dashboard [--port 8765]
    uv run python -m tools.dashboard --paddle-key-env PADDLE_LIVE_API_KEY_ZAAHEEN
    uv run python -m tools.dashboard --github-token-env GITHUB_READ_TOKEN_YAQZAN
    uv run python -m tools.dashboard --model-url http://127.0.0.1:8766
    uv run python -m tools.dashboard --bundles ~/.claude/plugins
    uv run python -m tools.dashboard --switches     # after: uv run python -m tools.laptop_db up
    uv run python -m tools.dashboard --switches --enrol-passkey   # once, to set up the passkey
    uv run python -m tools.dashboard --uptime projects/uptime.yaml
    uv run python -m tools.dashboard --export-contract   # refresh the UI's contract fixture
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from tools import laptop_db
from tools.chat_tools import Switched, model_chat, tools_for
from tools.dashboard_sources import (
    GITHUB_REPOS,
    Clock,
    github_portfolio,
    live_portfolio,
    usable_secret,
    utc_now,
)
from tools.dashboard_switches import LaptopSwitches, Prepare, connect, off_connectors
from tools.dashboard_uptime import UptimeListError, load_uptime, uptime_portfolio
from yaqzan.core.api import LOCAL_HOSTS, ChatBackend, Portfolio, SamplePortfolio, create_app
from yaqzan.core.api.code import CARD_ID as CODE_CARD
from yaqzan.core.api.registry import RegistryPage, registry_page, uses_from_cards
from yaqzan.core.api.uptime import CARD_ID as UPTIME_CARD
from yaqzan.core.auth import Site
from yaqzan.core.llm_gateway import Tool
from yaqzan.core.policy import Standing
from yaqzan.core.registry import Registry, load_registry
from yaqzan.core.sdk import SiteRule

UI_ROOT = Path(__file__).resolve().parent.parent / "apps" / "dashboard"
UI_BUILD = UI_ROOT / "dist"
CONTRACT_FIXTURE = UI_ROOT / "src" / "contract" / "sample.json"
PLUGINS_ROOT = UI_ROOT.parent.parent / "src" / "yaqzan" / "plugins"
CONTRACT_TIME = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Which card each connector feeds, on which projects (handoffs/CONNECTORS-CHECKLIST).
# Uptime is added per run, for the projects on the local list.
CONNECTOR_CARDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "paddle": ("paddle", ("zaaheen",)),
    "github": (CODE_CARD, tuple(GITHUB_REPOS)),
}

type UptimeRules = dict[str, tuple[SiteRule, ...]]


def wiring_for(uptime: UptimeRules) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Which card each connector feeds on this run: uptime only for the projects on its list."""
    return {**CONNECTOR_CARDS, "uptime": (UPTIME_CARD, tuple(uptime))} if uptime else dict(CONNECTOR_CARDS)


def load_plugins(bundles: Sequence[Path] = ()) -> Registry:
    """Everything on disk, read once; anything that could not be read is said."""
    registry = load_registry(PLUGINS_ROOT, bundles)
    for problem in registry.problems:
        print(f"dashboard: the {problem.kind.value} at {problem.path} was not loaded: {problem.message}")
    return registry


def registry_for(
    portfolio: Portfolio,
    bundles: Sequence[Path] = (),
    registry: Registry | None = None,
    uptime: UptimeRules | None = None,
) -> RegistryPage:
    """The Customize page: what is on disk, with each connector judged from the cards it feeds."""
    found = load_plugins(bundles) if registry is None else registry
    return registry_page(found, uses_from_cards(portfolio.project, wiring_for(uptime or {})))


def contract_sample() -> str:
    """The sample API answers as JSON, for the UI's tests to check against its zod shapes."""
    portfolio = SamplePortfolio(CONTRACT_TIME)
    projects = portfolio.projects()
    answers = {
        "projects": [p.model_dump(mode="json") for p in projects],
        "details": {p.id: _detail_json(portfolio, p.id) for p in projects},
        "models": portfolio.models().model_dump(mode="json"),
        "registry": registry_for(portfolio).model_dump(mode="json"),
    }
    return json.dumps(answers, indent=2, ensure_ascii=False) + "\n"


def _detail_json(portfolio: SamplePortfolio, project_id: str) -> object:
    detail = portfolio.project(project_id)
    if detail is None:  # pragma: no cover - ids come from the same portfolio
        raise LookupError(project_id)
    return detail.model_dump(mode="json")


class _NotStartedError(Exception):
    """A requested real-data read failed, after saying why."""


def _real_data(  # noqa: PLR0913  # the options, the outside world, and what is switched off or listed
    ns: argparse.Namespace,
    env: Mapping[str, str],
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
    *,
    off: frozenset[str] = frozenset(),
    uptime: UptimeRules | None = None,
) -> tuple[Portfolio | None, list[str]]:
    """The portfolio with every requested real source laid over the sample, and what is real.

    A connector in ``off`` (switched off on the Customize page) is not read at all.
    """
    portfolio: Portfolio | None = None
    live: list[str] = []
    asked_for = (("paddle", ns.paddle_key_env), ("github", ns.github_token_env), ("uptime", uptime))
    for connector, asked in asked_for:
        if asked and connector in off:
            print(f"dashboard: {connector} is switched off on the Customize page, so it was not read.")
    if ns.paddle_key_env and "paddle" not in off:
        portfolio = live_portfolio(ns.paddle_key_env, env, clock=clock, transport=transport)
        if portfolio is None:
            raise _NotStartedError
        live.append("Paddle for Zaaheen")
    if ns.github_token_env and "github" not in off:
        base = portfolio or SamplePortfolio(clock())
        portfolio = github_portfolio(base, ns.github_token_env, env, clock=clock, transport=transport)
        if portfolio is None:
            raise _NotStartedError
        live.append("GitHub for every project")
    if uptime and "uptime" not in off:
        base = portfolio or SamplePortfolio(clock())
        portfolio = uptime_portfolio(base, uptime, clock=clock, transport=transport)
        live.append("uptime checks")
    return portfolio, live


def _uptime_rules(ns: argparse.Namespace, clock: Clock) -> UptimeRules:
    """The local address list, if one was given (``--uptime``); a broken list stops the start, saying why."""
    if ns.uptime is None:
        return {}
    projects = {p.id for p in SamplePortfolio(clock()).projects()}
    try:
        return load_uptime(ns.uptime, projects)
    except UptimeListError as exc:
        print(f"dashboard: {exc}.")
        raise _NotStartedError from None


def main(  # noqa: PLR0913  # the real world (files, server, env, network, clock) is injectable for tests
    argv: Sequence[str] | None = None,
    *,
    static_dir: Path = UI_BUILD,
    serve: Callable[[FastAPI, str, int], None] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = utc_now,
    prepare: Prepare = laptop_db.prepare,
) -> int:
    """Start the dashboard; 1 if the UI is not built, a read failed, or a token or the database is missing."""
    ns = _parser().parse_args(argv)
    done = _preflight(ns, static_dir)
    if done is not None:
        return done
    env = os.environ if environ is None else environ
    return _start(
        ns, env, static_dir=static_dir, serve=serve, transport=transport, clock=clock, prepare=prepare
    )


def _parser() -> argparse.ArgumentParser:
    """The command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--export-contract", action="store_true", help="rewrite the UI's contract fixture")
    parser.add_argument("--paddle-key-env", help="env var holding the live READ-ONLY Zaaheen Paddle key")
    parser.add_argument("--github-token-env", help="env var holding the READ-ONLY GitHub token")
    parser.add_argument("--model-url", help="the chat's model server, e.g. http://127.0.0.1:8766")
    parser.add_argument(
        "--bundles", type=Path, action="append", default=[], help="show plugin bundles in DIR"
    )
    parser.add_argument(
        "--switches", action="store_true", help="on/off switches, kept in the laptop database"
    )
    parser.add_argument(
        "--enrol-passkey", action="store_true", help="allow the one-time passkey set-up (needs --switches)"
    )
    parser.add_argument(
        "--uptime", type=Path, help="check the addresses in this local list (projects/uptime.yaml)"
    )
    return parser


def _preflight(ns: argparse.Namespace, static_dir: Path) -> int | None:
    """Export the contract, or refuse an odd port or a missing UI; ``None`` to go on and serve."""
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
    if ns.enrol_passkey and not ns.switches:
        print("dashboard: --enrol-passkey needs --switches (the passkey is kept in the laptop database).")
        return 1
    return None


def _start(  # noqa: PLR0913  # the parsed options and the injectable outside world
    ns: argparse.Namespace,
    env: Mapping[str, str],
    *,
    static_dir: Path,
    serve: Callable[[FastAPI, str, int], None] | None,
    transport: httpx.AsyncBaseTransport | None,
    clock: Clock,
    prepare: Prepare,
) -> int:
    """Read what was asked for, wire the switches and the chat, then serve."""
    registry = load_plugins([b.expanduser() for b in ns.bundles])
    connected = connect(registry, prepare) if ns.switches else None
    if ns.switches and connected is None:
        return 1
    try:
        uptime = _uptime_rules(ns, clock)
        off = off_connectors(connected[1], registry) if connected else frozenset[str]()
        portfolio, live = _real_data(ns, env, clock, transport, off=off, uptime=uptime)
    except _NotStartedError:
        return 1
    chosen = portfolio or SamplePortfolio(clock())
    switches = None
    if connected is not None:
        code = secrets.token_hex(4) if ns.enrol_passkey else None
        switches = LaptopSwitches(
            connected[0],
            registry,
            uses_from_cards(chosen.project, wiring_for(uptime)),
            clock,
            site=Site(origin=f"http://localhost:{ns.port}"),
            enrol_code=code,
        )
        if code is not None:
            print(f"dashboard: passkey set-up is open. The code to type on the Customize page is {code}")
    chat: ChatBackend | None = None
    if ns.model_url:
        token = env.get(ns.github_token_env or "", "")
        chat = _chat(
            ns.model_url, token, registry=registry, switches=switches, clock=clock, transport=transport
        )
        if chat is None:
            return 1
    page = registry_for(chosen, registry=registry, uptime=uptime)
    app = create_app(
        chosen, static_dir=static_dir, allowed_hosts=LOCAL_HOSTS, chat=chat, registry=page, switches=switches
    )
    what = f"real numbers from {' and '.join(live)}, sample data elsewhere" if live else "sample data"
    if chat is not None:
        what += "; chat connected to the local model"
    if switches is not None:
        what += "; switches remembered in the laptop database"
    print(f"dashboard: open http://localhost:{ns.port} ({what}; press Ctrl+C to stop)")
    (serve or _serve)(app, HOST, ns.port)
    return 0


def _chat(  # noqa: PLR0913  # the model, the token, what exists, what is on, and the outside world
    model_url: str,
    raw_token: str,
    *,
    registry: Registry,
    switches: LaptopSwitches | None,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
) -> ChatBackend | None:
    """The chat, with tools for what is on (with switches, it asks them before every question)."""
    raw = raw_token.strip()
    token = SecretStr(raw) if raw and usable_secret(raw) else None
    if switches is None:
        tools, skills = tools_for(registry, PLUGINS_ROOT, token, GITHUB_REPOS, clock, transport=transport)
        return model_chat(model_url, transport, tools, skills)

    def build(standing: Standing) -> tuple[list[Tool], str]:
        return tools_for(
            registry, PLUGINS_ROOT, token, GITHUB_REPOS, clock, transport=transport, standing=standing
        )

    return model_chat(model_url, transport, switched=Switched(build=build, standing=switches.standing))


def _serve(app: FastAPI, host: str, port: int) -> None:  # pragma: no cover - starts a real server
    uvicorn.run(app, host=host, port=port, log_level="warning", server_header=False)


if __name__ == "__main__":
    sys.exit(main())
