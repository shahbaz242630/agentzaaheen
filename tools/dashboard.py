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
Each workspace page's "Built with" panel also reads, with the same token, the
repository's languages and the providers code finds in it, again each time the
page opens if the repository changed (``tools.dashboard_profile``).

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
(``tools.dashboard_uptime``) and shows them on each project's Uptime card,
re-checking every 5 minutes; with ``--ntfy-topic-env NAME`` a site going
down (two failed checks in a row) or coming back is pushed to the
Chairman's phone (ADR-031). With ``--otel-token-env NAME --otel-endpoint URL
--otel-user ID`` every API request, outside call and database use is traced
and sent to the trace store (``tools.dashboard_tracing``, ADR-032). With
``--fixes FILE`` the chat can read each project part's fix guide
(``tools.fix_guides``, the local ``projects/fixes.yaml``). With ``--switches``
each workspace also gets a daily summary at 05:00, written by code, and the
Board's briefing goes to the phone at 07:00 (with ``--ntfy-topic-env``), both
in ``--briefing-zone`` (default Asia/Dubai) (``tools.dashboard_summaries``).
With ``--briefing-ping-env NAME`` each sent briefing pings that Healthchecks
check, which alerts the phone when no briefing arrives. With ``--log-dir DIR``
every log line (redacted, as on screen) is also kept in DIR, one file a day,
for 30 days, and the Logs page reads them (ADR-034; on the laptop: ``~/AppData/Local/AgentZaaheen/logs``).

Usage:
    cd apps/dashboard && npm ci && npm run build     # once, and after UI changes
    uv run python -m tools.dashboard [--port 8765]
    uv run python -m tools.dashboard --paddle-key-env PADDLE_LIVE_API_KEY_ZAAHEEN
    uv run python -m tools.dashboard --github-token-env GITHUB_READ_TOKEN_AGENTZAAHEEN
    uv run python -m tools.dashboard --model-url http://127.0.0.1:8766
    uv run python -m tools.dashboard --bundles ~/.claude/plugins
    uv run python -m tools.dashboard --switches     # after: uv run python -m tools.laptop_db up
    uv run python -m tools.dashboard --switches --enrol-passkey   # once, to set up the passkey
    uv run python -m tools.dashboard --uptime projects/uptime.yaml --ntfy-topic-env AGENTZAAHEEN_NTFY_TOPIC
    uv run python -m tools.dashboard --supabase projects/supabase.yaml
    uv run python -m tools.dashboard --model-url http://127.0.0.1:8766 --fixes projects/fixes.yaml
    uv run python -m tools.dashboard --log-dir ~/AppData/Local/AgentZaaheen/logs   # keep 30 days of logs
    uv run python -m tools.dashboard --export-contract   # refresh the UI's contract fixture
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import zoneinfo
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from agentzaaheen.core.api import LOCAL_HOSTS, ChatBackend, Portfolio, SamplePortfolio, create_app
from agentzaaheen.core.api.code import CARD_ID as CODE_CARD
from agentzaaheen.core.api.registry import RegistryPage, registry_page, uses_from_cards
from agentzaaheen.core.api.uptime import CARD_ID as UPTIME_CARD
from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.auth import Site
from agentzaaheen.core.llm_gateway import Tool
from agentzaaheen.core.net import HEALTHCHECKS_PING_HOST, ModelClient, NetError, NetPolicy, check_ping_url
from agentzaaheen.core.policy import Standing
from agentzaaheen.core.registry import Registry, load_registry
from agentzaaheen.core.registry.roles import RoleLibrary, load_roles
from agentzaaheen.core.scheduler import DEFAULT_ZONE
from agentzaaheen.core.sdk import SiteRule, span
from agentzaaheen.core.store import ScoutRun, WorkspaceRow
from agentzaaheen.core.telemetry import LogFile, LogFolder
from tools import laptop_db, vault_key
from tools.chat_tools import LOCAL_MODEL_ID, Switched, model_chat, tools_for
from tools.dashboard_adding import LaptopAdding
from tools.dashboard_profile import LaptopProfiles, Repos
from tools.dashboard_scout import LaptopScout
from tools.dashboard_sources import (
    GITHUB_REPOS,
    Clock,
    ListNotUsableError,
    github_portfolio,
    live_portfolio,
    usable_secret,
    utc_now,
)
from tools.dashboard_summaries import AlertLog, LaptopSummaries
from tools.dashboard_supabase import SupabaseProject, supabase_list, supabase_portfolio
from tools.dashboard_switches import LaptopClaudeKeys, LaptopSwitches, Prepare, connect, off_connectors
from tools.dashboard_tracing import TracingNotStartedError, add_tracing_options, tracing_from
from tools.dashboard_uptime import ntfy_topic, start_watch, uptime_list, uptime_portfolio
from tools.fix_guides import Project, fixes_list

UI_ROOT = Path(__file__).resolve().parent.parent / "apps" / "dashboard"
UI_BUILD = UI_ROOT / "dist"
CONTRACT_FIXTURE = UI_ROOT / "src" / "contract" / "sample.json"
PLUGINS_ROOT = UI_ROOT.parent.parent / "src" / "agentzaaheen" / "plugins"
# The role packs stay off GitHub (docs/ is local only); on the server they live in their own folder.
ROLES_ROOT = UI_ROOT.parent.parent / "docs" / "hr" / "roles"
CONTRACT_TIME = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
HOST = "127.0.0.1"
SCOUT_TIMEOUT_S = 900.0  # one scout answer takes about 5 minutes on the server's CPU
DEFAULT_PORT = 8765
# Which card each connector feeds, on which projects (handoffs/CONNECTORS-CHECKLIST).
# Uptime is added per run, for the projects on the local list.
CONNECTOR_CARDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "paddle": ("paddle", ("zaaheen",)),
    "github": (CODE_CARD, tuple(GITHUB_REPOS)),
}

type UptimeRules = dict[str, tuple[SiteRule, ...]]


type SupabaseList = dict[str, SupabaseProject]


def wiring_for(
    uptime: UptimeRules, backends: SupabaseList | None = None
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Which card each connector feeds on this run (uptime and Supabase: only the projects listed)."""
    wiring = dict(CONNECTOR_CARDS)
    if uptime:
        wiring["uptime"] = (UPTIME_CARD, tuple(uptime))
    if backends:
        wiring["supabase"] = ("supabase", tuple(backends))
    return wiring


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
    backends: SupabaseList | None = None,
) -> RegistryPage:
    """The Customize page: what is on disk, with each connector judged from the cards it feeds."""
    found = load_plugins(bundles) if registry is None else registry
    return registry_page(found, uses_from_cards(portfolio.project, wiring_for(uptime or {}, backends)))


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
    backends: SupabaseList | None = None,
    alerts: AlertLog | None = None,
) -> tuple[Portfolio | None, list[str]]:
    """The portfolio with every requested real source laid over the sample, and what is real.

    A connector in ``off`` (switched off on the Customize page) is not read at all.
    """
    portfolio: Portfolio | None = None
    live: list[str] = []
    asked_for = (
        ("paddle", ns.paddle_key_env),
        ("github", ns.github_token_env),
        ("uptime", uptime),
        ("supabase", backends),
    )
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
        names = {p.id: p.name for p in base.projects()}
        watcher = start_watch(
            uptime,
            names,
            env,
            ns.ntfy_topic_env,
            clock=clock,
            transport=transport,
            on_alerts=alerts.add if alerts is not None else None,
        )
        if watcher is None:
            raise _NotStartedError
        portfolio = uptime_portfolio(base, watcher, clock=clock)
        watcher.start()
        live.append("uptime checks")
    if backends and "supabase" not in off:
        base = portfolio or SamplePortfolio(clock())
        portfolio = supabase_portfolio(base, backends, env, clock=clock, transport=transport)
        live.append("Supabase")
    return portfolio, live


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
    log_file = _log_file(ns.log_dir)
    if log_file is False:
        return 1
    try:
        tracing = tracing_from(ns, env)
    except TracingNotStartedError:
        if log_file is not None:
            log_file.stop()
        return 1
    try:
        return _start(
            ns, env, static_dir=static_dir, serve=serve, transport=transport, clock=clock, prepare=prepare
        )
    finally:
        if tracing is not None:
            tracing.shutdown()
        if log_file is not None:
            log_file.stop()


def _log_file(folder: Path | None) -> LogFile | Literal[False] | None:
    """The started 30-day log file; ``None`` if not asked for; ``False`` once it said why it cannot start."""
    if folder is None:
        return None
    try:
        log_file = LogFile(folder.expanduser()).start()
    except OSError as exc:
        print(f"dashboard: cannot keep log files in {folder} ({type(exc).__name__}); pick another --log-dir.")
        return False
    print(f"dashboard: log lines are kept in {log_file.folder} for 30 days.")
    return log_file


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
        "--vault-key",
        type=Path,
        default=vault_key.KEY_FILE,
        help="the vault's master key file (with --switches: saving the Claude key needs it)",
    )
    parser.add_argument(
        "--enrol-passkey", action="store_true", help="allow the one-time passkey set-up (needs --switches)"
    )
    parser.add_argument(
        "--uptime", type=Path, help="check the addresses in this local list (projects/uptime.yaml)"
    )
    parser.add_argument("--ntfy-topic-env", help="env var holding the secret ntfy topic for uptime alerts")
    parser.add_argument("--supabase", type=Path, help="read the Supabase projects in this local list")
    parser.add_argument("--fixes", type=Path, help="give the chat the fix guides in this local file")
    parser.add_argument(
        "--roles",
        type=Path,
        default=ROLES_ROOT,
        help="the role library for the Agents tab (needs --switches)",
    )
    parser.add_argument(
        "--briefing-ping-env",
        help="env var holding the Healthchecks ping address told after each sent briefing (needs --switches)",
    )
    parser.add_argument(
        "--briefing-zone",
        default=DEFAULT_ZONE,
        help="the time zone of the 05:00 summaries and the 07:00 briefing (needs --switches)",
    )
    parser.add_argument(
        "--log-dir", type=Path, help="also keep every log line in this folder, one file a day, for 30 days"
    )
    add_tracing_options(parser)
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
    try:
        zoneinfo.ZoneInfo(ns.briefing_zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        print(f"dashboard: {ns.briefing_zone!r} is not a time zone Agent Zaaheen knows (try Asia/Dubai).")
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
        projects = {p.id for p in SamplePortfolio(clock()).projects()}
        uptime = uptime_list(ns.uptime, projects)
        backends = supabase_list(ns.supabase, projects)
        fixes = fixes_list(ns.fixes, projects)
        off = off_connectors(connected.standing, registry) if connected else frozenset[str]()
        alerts = AlertLog(clock)  # the phone alerts, counted for the daily summaries
        with span("dashboard start"):  # every start-up read in one trace
            portfolio, live = _real_data(
                ns, env, clock, transport, off=off, uptime=uptime, backends=backends, alerts=alerts
            )
    except (_NotStartedError, ListNotUsableError):
        return 1
    chosen = portfolio or SamplePortfolio(clock())
    switches = None
    library = _library(ns.roles)
    if connected is not None:
        code = secrets.token_hex(4) if ns.enrol_passkey else None
        switches = LaptopSwitches(
            connected.url,
            registry,
            uses_from_cards(chosen.project, wiring_for(uptime, backends)),
            clock,
            site=Site(origin=f"http://localhost:{ns.port}"),
            enrol_code=code,
            library=library,
            keyring=vault_key.load_keyring(ns.vault_key),
        )
        if code is not None:
            print(f"dashboard: passkey set-up is open. The code to type on the Customize page is {code}")
    # Adding a project lists the repositories the GitHub token covers (unless GitHub is switched off),
    # and each workspace page shows what its repository is built with.
    github_token = (env.get(ns.github_token_env or "", "") if "github" not in off else "").strip()
    usable = SecretStr(github_token) if github_token and usable_secret(github_token) else None
    adding = LaptopAdding(switches, usable, transport) if switches is not None else None
    profiles = LaptopProfiles(usable, _repos_of(switches), clock, transport)
    scout = _scout(
        ns.model_url,
        switches,
        token=usable,
        library=library,
        profiles=profiles,
        clock=clock,
        transport=transport,
    )
    # The workspaces the door knows (ADR-039): the laptop database's, or the fixed five without it.
    known = connected.workspaces if connected else laptop_db.LAPTOP_WORKSPACES
    # Read again whenever a workspace is archived or brought back.
    directory = WorkspaceDirectory(known)
    chat: ChatBackend | None = None
    if ns.model_url:
        token = env.get(ns.github_token_env or "", "")
        chat = _chat(
            ns.model_url,
            token,
            registry=registry,
            switches=switches,
            workspaces=directory,
            clock=clock,
            transport=transport,
            fixes=fixes,
            logs=LogFolder(ns.log_dir.expanduser()) if ns.log_dir is not None else None,
        )
        if chat is None:
            return 1
    summaries = _summaries(
        ns,
        env,
        switches,
        portfolio=chosen,
        directory=directory,
        alerts=alerts,
        watched=frozenset(uptime) if uptime and "uptime" not in off else frozenset(),
        clock=clock,
        transport=transport,
    )
    page = registry_for(chosen, registry=registry, uptime=uptime, backends=backends)
    app = create_app(
        chosen,
        static_dir=static_dir,
        allowed_hosts=LOCAL_HOSTS,
        chat=chat,
        registry=page,
        switches=switches,
        workspaces=directory,
        agents=switches,
        removal=switches,
        adding=adding,
        profiles=profiles,
        scout=scout,
        summaries=summaries,
        logs=LogFolder(ns.log_dir.expanduser()) if ns.log_dir is not None else None,
        claude_key=LaptopClaudeKeys(switches) if switches is not None else None,
        on_start=summaries.start if summaries is not None else None,
        on_close=_closer(switches, scout, summaries),
    )
    what = f"real numbers from {' and '.join(live)}, sample data elsewhere" if live else "sample data"
    if chat is not None:
        what += "; chat connected to the local model"
    if switches is not None:
        what += "; switches remembered in the laptop database"
    if summaries is not None:
        what += f"; daily summaries at 05:00 and the briefing at 07:00 ({ns.briefing_zone})"
    print(f"dashboard: open http://localhost:{ns.port} ({what}; press Ctrl+C to stop)")
    (serve or _serve)(app, HOST, ns.port)
    return 0


def _scout(  # noqa: PLR0913  # the model, the database, GitHub, the roles, more facts, the outside world
    model_url: str | None,
    switches: LaptopSwitches | None,
    *,
    token: SecretStr | None,
    library: RoleLibrary,
    profiles: LaptopProfiles,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
) -> LaptopScout | None:
    """The scout, if there is a database and a model (it needs both to save and to think)."""
    if switches is None or not model_url:
        return None
    try:
        model = ModelClient(model_url, timeout_s=SCOUT_TIMEOUT_S, transport=transport)
    except NetError:
        return None  # the chat says why the address was refused
    return LaptopScout(
        control=switches.scout_control,
        repos=switches.repos_in_use,
        token=token,
        model=model,
        model_id=LOCAL_MODEL_ID,
        library=library,
        clock=clock,
        profiles=profiles,
        transport=transport,
    )


def _closer(
    switches: LaptopSwitches | None, scout: LaptopScout | None, summaries: LaptopSummaries | None = None
) -> Callable[[], Awaitable[None]] | None:
    """What to close when the server stops: the morning job and the scout's runs, then the database."""
    if switches is None:
        return None

    async def close() -> None:
        if summaries is not None:
            await summaries.close()
        if scout is not None:
            await scout.close()
        await switches.close()

    return close


def _summaries(  # noqa: PLR0913  # the options, the database, the facts' sources, the outside world
    ns: argparse.Namespace,
    env: Mapping[str, str],
    switches: LaptopSwitches | None,
    *,
    portfolio: Portfolio,
    directory: WorkspaceDirectory,
    alerts: AlertLog,
    watched: frozenset[str],
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
) -> LaptopSummaries | None:
    """The daily summaries and the morning briefing, if there is a database to keep them in."""
    if switches is None:
        return None
    topic = ntfy_topic(env, ns.ntfy_topic_env) if ns.ntfy_topic_env else None
    if topic is None:
        print("dashboard: no ntfy topic, so the morning briefing is kept on the dashboard only.")

    async def scout_of(workspace: WorkspaceRow) -> ScoutRun | None:
        return await (await switches.scout_control()).latest(workspace)

    return LaptopSummaries(
        control=switches.summary_control,
        portfolio=portfolio,
        directory=directory,
        clock=clock,
        zone=zoneinfo.ZoneInfo(ns.briefing_zone),
        agents=switches.view,
        scout=scout_of,
        claude=switches.claude_lines,
        alerts=alerts,
        watched=watched,
        topic=topic,
        ping=_briefing_ping(env, ns.briefing_ping_env, topic),
        transport=transport,
    )


def _briefing_ping(env: Mapping[str, str], ping_env: str | None, topic: SecretStr | None) -> SecretStr | None:
    """The briefing's Healthchecks ping address, if set and usable (said on the console, never shown)."""
    if not ping_env:
        return None
    raw = env.get(ping_env, "").strip()
    if not raw:
        print(f"dashboard: {ping_env} is not set, so Healthchecks will not hear about the briefing.")
        return None
    try:
        check_ping_url(SecretStr(raw), NetPolicy(allowed_hosts=frozenset({HEALTHCHECKS_PING_HOST})))
    except NetError:
        print(f"dashboard: {ping_env} is not a Healthchecks ping address (https://hc-ping.com/<uuid>).")
        return None
    if topic is None:
        print("dashboard: the briefing is not sent to the phone, so Healthchecks will alert every morning.")
    return SecretStr(raw)


def _repos_of(switches: LaptopSwitches | None) -> Repos:
    """Where each workspace's repository is read from: the laptop database, or the fixed list without it."""
    if switches is not None:
        return switches.repos_in_use

    async def fixed() -> Mapping[str, str]:
        return laptop_db.LAPTOP_REPOS

    return fixed


def _chat(  # noqa: PLR0913  # the model, the token, what exists, what is on, and the outside world
    model_url: str,
    raw_token: str,
    *,
    registry: Registry,
    switches: LaptopSwitches | None,
    workspaces: WorkspaceDirectory,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
    fixes: Mapping[str, Project],
    logs: LogFolder | None,
) -> ChatBackend | None:
    """The chat, with tools for what is on (with switches, it asks them before every question)."""
    raw = raw_token.strip()
    token = SecretStr(raw) if raw and usable_secret(raw) else None
    if switches is None:
        tools, skills = tools_for(
            registry, PLUGINS_ROOT, token, GITHUB_REPOS, clock, transport=transport, fixes=fixes, logs=logs
        )
        return model_chat(model_url, transport, tools, skills)

    def build(standing: Standing) -> tuple[list[Tool], str]:
        return tools_for(
            registry,
            PLUGINS_ROOT,
            token,
            GITHUB_REPOS,
            clock,
            transport=transport,
            standing=standing,
            fixes=fixes,
            logs=logs,
        )

    # Board inspection needs the audit log (the laptop database), so it exists only with switches.
    return model_chat(
        model_url,
        transport,
        switched=Switched(build=build, standing=switches.standing),
        inspect=switches.recorder(workspaces),
    )


def _library(folder: Path) -> RoleLibrary:
    """The role library, saying how many roles it holds and which were refused, and why."""
    library = load_roles(folder)
    print(f"dashboard: {len(library.packs)} roles in the library ({folder.name}).")
    for problem in library.problems:
        print(f"dashboard: role {problem.id or folder.name} not used: {problem.message}.")
    return library


def _serve(app: FastAPI, host: str, port: int) -> None:  # pragma: no cover - starts a real server
    uvicorn.run(app, host=host, port=port, log_level="warning", server_header=False)


if __name__ == "__main__":
    sys.exit(main())
