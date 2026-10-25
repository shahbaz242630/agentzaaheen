"""The dashboard's web server: a read-only JSON API plus the built web UI.

Only GET routes exist, plus a few POSTs: the chat (F05, ADR-024), which reads
the cards and asks a model and changes nothing, and the Customize page's
switches (ADR-030), which can only switch an item off, ask for it to be
switched on, withdraw that request, set up the Chairman's passkey once, and
approve (with a Windows Hello signature for that one request, ADR-SEC-007)
or refuse a request. Every POST answers only same-origin
JSON requests that carry the ``X-Yaqzan-Request`` header, so another web page
cannot use it. Every response carries a strict Content-Security-Policy
(SECURITY: no inline script, no remote images), and only the expected host
names are answered, which stops a hostile web page reaching this server
through DNS rebinding while it runs on the laptop. Each API request is one
server span named by its route template (never the filled-in path), and the
answer carries its ``traceparent``, so any error shown leads to its trace
(ADR-032). A ``traceparent`` sent to us is ignored: every request starts its
own trace.

Each project is a workspace (ADR-039) with its own address,
``/api/workspaces/{slug}``. The workspace is chosen there, once, by the door
(:class:`WorkspaceDirectory`), from the server's own list; no request body
names one. The workspace's chat sees only that workspace's facts and tools.
``/api/chat`` is the Board's chat: every workspace's summary line, and no
tool that reads one workspace's data.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid5

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from yaqzan.core.api.adding import WorkspaceAdding, adding_routes
from yaqzan.core.api.agents import AgentSwitches, agent_routes
from yaqzan.core.api.facts import portfolio_facts, project_facts, uses_sample
from yaqzan.core.api.profile import WorkspaceProfiles, profile_routes
from yaqzan.core.api.registry import (
    EnrolAnswer,
    EnrolCode,
    ItemRef,
    PasskeyOptions,
    PasskeyStatus,
    RegistryPage,
    SignedAnswer,
)
from yaqzan.core.api.removal import WorkspaceRemoval, removal_routes
from yaqzan.core.api.scout import WorkspaceScout, scout_routes
from yaqzan.core.api.summaries import WorkspaceSummaries, summary_routes
from yaqzan.core.api.views import (
    ChatAnswer,
    ChatQuestion,
    Health,
    ModelList,
    ProjectDetail,
    ProjectSummary,
)
from yaqzan.core.api.workspaces import SCOPE_KEY, InWorkspace, WorkspaceDirectory
from yaqzan.core.auth import PasskeyError
from yaqzan.core.llm_gateway import ChatReply
from yaqzan.core.policy import Verdict
from yaqzan.core.registry import Kind
from yaqzan.core.sdk import SourceError, elapsed_ms, failed, get_logger, say, span, traceparent
from yaqzan.core.store import StoreError, WorkspaceRow

LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
_SAMPLE_NS = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # ids for sample workspaces (no database)

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


class Portfolio(Protocol):
    """Where the dashboard's numbers come from (sample data now, stored snapshots later)."""

    def projects(self) -> tuple[ProjectSummary, ...]:
        """The projects, in display order."""
        ...

    def project(self, project_id: str) -> ProjectDetail | None:
        """One project's cards, or ``None`` if unknown."""
        ...

    def models(self) -> ModelList:
        """The chat's model menu."""
        ...


class ChatBackend(Protocol):
    """The one model the chat is connected to (``llm_gateway.FactChat`` in real use)."""

    @property
    def model_id(self) -> str:
        """Which entry of the model menu this is."""
        ...

    async def ask(self, question: str, facts: str, workspace: str | None = None) -> ChatReply:
        """The answer to ``question``, from ``facts`` and read-only tools, and what it used.

        ``workspace`` is the one workspace asked in (its slug), or ``None`` for the Board.
        """
        ...


class RegistrySwitches(Protocol):
    """The Customize page's switches (``api.registry.DashboardSwitches`` in real use)."""

    async def page(self) -> RegistryPage:
        """The page as stored now."""
        ...

    async def switch_off(self, kind: Kind, item_id: str) -> Verdict:
        """Switch one item off."""
        ...

    async def request_on(self, kind: Kind, item_id: str) -> Verdict:
        """Ask for one item to be switched on."""
        ...

    async def withdraw(self, request_id: UUID) -> Verdict:
        """Withdraw a waiting request."""
        ...

    async def passkey(self) -> PasskeyStatus:
        """Whether the passkey exists and may be set up now."""
        ...

    async def enrol_options(self, code: str) -> dict[str, Any]:
        """Options to make the passkey."""
        ...

    async def enrol(self, code: str, credential: dict[str, Any]) -> None:
        """Keep the new passkey."""
        ...

    async def approve_options(self, request_id: UUID) -> dict[str, Any]:
        """Options to sign for one request."""
        ...

    async def approve(self, request_id: UUID, credential: dict[str, Any]) -> Verdict:
        """Approve with a signed answer."""
        ...

    async def refuse(self, request_id: UUID) -> Verdict:
        """Refuse a waiting request."""
        ...


REQUEST_HEADER = "X-Yaqzan-Request"
_SERVER_ERROR = 500


def _connected(models: ModelList, chat: ChatBackend | None) -> ModelList:
    """The menu with exactly the connected model marked available."""
    return ModelList(
        models=tuple(
            m.model_copy(update={"available": chat is not None and m.id == chat.model_id})
            for m in models.models
        )
    )


def _refuse_foreign(request: Request) -> None:
    """Only our own page may ask: same origin, JSON, and our custom header (which forces a CORS preflight)."""
    if request.headers.get(REQUEST_HEADER) != "1":
        raise HTTPException(status_code=403, detail="chat.forbidden")
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
        raise HTTPException(status_code=403, detail="chat.forbidden")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="chat.json_only")


async def _stored[T](call: Awaitable[T]) -> T:
    """Await a database-backed call; a database failure becomes 503 ``registry.store_unavailable``."""
    try:
        return await call
    except StoreError as exc:
        say(get_logger("yaqzan.api"), "registry_store_failed", code=exc.code.value)
        raise HTTPException(status_code=503, detail="registry.store_unavailable") from None


def _ready(switches: RegistrySwitches | None) -> RegistrySwitches:
    if switches is None:
        raise HTTPException(status_code=503, detail="registry.read_only")
    return switches


def _registry_routes(app: FastAPI, page: RegistryPage, switches: RegistrySwitches | None) -> None:
    """The Customize page: read it, and (with a database) switch off, ask, withdraw, refuse, approve."""

    async def change(act: Callable[[RegistrySwitches], Awaitable[Verdict]]) -> RegistryPage:
        ready = _ready(switches)
        verdict = await _stored(act(ready))
        if verdict is not Verdict.DONE:
            raise HTTPException(status_code=409, detail=verdict.value)
        return await _stored(ready.page())

    @app.get("/api/registry")
    async def get_registry() -> RegistryPage:
        return page if switches is None else await _stored(switches.page())

    @app.post("/api/registry/switch-off", dependencies=[Depends(_refuse_foreign)])
    async def switch_off(item: ItemRef) -> RegistryPage:
        return await change(lambda s: s.switch_off(item.kind, item.id))

    @app.post("/api/registry/requests", dependencies=[Depends(_refuse_foreign)])
    async def request_on(item: ItemRef) -> RegistryPage:
        return await change(lambda s: s.request_on(item.kind, item.id))

    @app.post("/api/registry/requests/{request_id}/withdraw", dependencies=[Depends(_refuse_foreign)])
    async def withdraw(request_id: UUID) -> RegistryPage:
        return await change(lambda s: s.withdraw(request_id))

    @app.post("/api/registry/requests/{request_id}/refuse", dependencies=[Depends(_refuse_foreign)])
    async def refuse(request_id: UUID) -> RegistryPage:
        return await change(lambda s: s.refuse(request_id))

    @app.post("/api/registry/requests/{request_id}/approve", dependencies=[Depends(_refuse_foreign)])
    async def approve(request_id: UUID, answer: SignedAnswer) -> RegistryPage:
        return await change(lambda s: s.approve(request_id, answer.credential))


def _passkey_routes(app: FastAPI, switches: RegistrySwitches | None) -> None:
    """The passkey (ADR-SEC-007): its status, the one-time set-up, and options to sign one approval."""

    async def checked[T](call: Awaitable[T]) -> T:
        try:
            return await _stored(call)
        except PasskeyError as exc:
            status = 403 if exc.code in {"passkey.wrong_code", "passkey.enrol_closed"} else 409
            raise HTTPException(status_code=status, detail=exc.code) from None

    @app.post("/api/registry/requests/{request_id}/approve/options", dependencies=[Depends(_refuse_foreign)])
    async def approve_options(request_id: UUID) -> PasskeyOptions:
        return PasskeyOptions(options=await checked(_ready(switches).approve_options(request_id)))

    @app.get("/api/passkey")
    async def passkey() -> PasskeyStatus:
        if switches is None:
            return PasskeyStatus(enrolled=False, enrolment_open=False)
        return await _stored(switches.passkey())

    @app.post("/api/passkey/enrol/options", dependencies=[Depends(_refuse_foreign)])
    async def enrol_options(body: EnrolCode) -> PasskeyOptions:
        return PasskeyOptions(options=await checked(_ready(switches).enrol_options(body.code)))

    @app.post("/api/passkey/enrol", dependencies=[Depends(_refuse_foreign)])
    async def enrol(body: EnrolAnswer) -> PasskeyStatus:
        await checked(_ready(switches).enrol(body.code, body.credential))
        return await _stored(_ready(switches).passkey())


async def _with_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Security headers on every answer; for API requests also a server span and its ``traceparent``."""
    if not request.url.path.startswith("/api/"):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response
    method = request.method
    started = time.monotonic()
    with span(f"{method} /api", kind="server", attributes={"http.request.method": method}) as current:
        try:
            response = await call_next(request)
        except Exception:
            # Logged inside the span, so the line joins this request's trace.
            _said(method, _route_template(request), _SERVER_ERROR, started)
            raise
        route = _route_template(request)
        current.update_name(f"{method} {route}")
        current.set_attribute("http.route", route)
        current.set_attribute("http.response.status_code", response.status_code)
        if response.status_code >= _SERVER_ERROR:  # a server error, not a refusal
            failed(current, str(response.status_code))
        _said(method, route, response.status_code, started, _workspace(request))
        parent = traceparent()
    response.headers.update(SECURITY_HEADERS)
    response.headers["Cache-Control"] = "no-store"
    if parent is not None:
        response.headers["traceparent"] = parent
    return response


def _said(method: str, route: str, status: int, started: float, workspace: str | None = None) -> None:
    """The API door's one line per request (ADR-034): the route template, never the filled-in path.

    Inside a workspace the line names it (its slug, never anything the request carried).
    """
    log, duration_ms = get_logger("yaqzan.api"), elapsed_ms(started)
    line = {"method": method, "route": route, "http_status": status, "duration_ms": duration_ms}
    failed = status >= _SERVER_ERROR
    if workspace is None and failed:
        say(log, "api_failed", **line)
    elif workspace is None:
        say(log, "api_answered", **line)
    elif failed:
        say(log, "workspace_api_failed", workspace=workspace, **line)
    else:
        say(log, "workspace_api_answered", workspace=workspace, **line)


def _workspace(request: Request) -> str | None:
    """The workspace the door let this request into, or ``None`` (not a workspace route, or refused)."""
    found = request.scope.get(SCOPE_KEY)
    return found if isinstance(found, str) else None


def _no_data(workspace: WorkspaceRow) -> ProjectSummary:
    """A workspace with nothing to show yet: unknown, never "all fine"."""
    return ProjectSummary(
        id=workspace.slug,
        name=workspace.name,
        summary="No data yet",
        health=Health.UNKNOWN,
        escalations=0,
        sample=False,
    )


def _route_template(request: Request) -> str:
    """The matched route's template (``/api/projects/{project_id}``), never the filled-in path."""
    template = getattr(request.scope.get("route"), "path", None)
    return template if isinstance(template, str) and template else "unmatched"


def create_app(  # noqa: PLR0913  # the portfolio, then each optional part of the dashboard
    portfolio: Portfolio,
    *,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
    chat: ChatBackend | None = None,
    registry: RegistryPage | None = None,
    switches: RegistrySwitches | None = None,
    workspaces: WorkspaceDirectory | None = None,
    agents: AgentSwitches | None = None,
    removal: WorkspaceRemoval | None = None,
    adding: WorkspaceAdding | None = None,
    profiles: WorkspaceProfiles | None = None,
    scout: WorkspaceScout | None = None,
    summaries: WorkspaceSummaries | None = None,
    on_start: Callable[[], Awaitable[None]] | None = None,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Build the app. ``static_dir`` is the built web UI; without it only the API is served.

    ``workspaces`` is the server's list of workspaces (from the database). Without
    it, each project of the portfolio is a workspace (sample data, no database).
    ``agents`` is the Agents tab (with a database); without it the tab is read-only and empty.
    ``removal`` archives, brings back and deletes workspaces (with a database); without it, it is refused.
    ``adding`` adds a project from the Board (with a database); without it, it is refused.
    ``profiles`` reads what each workspace is built with (with a GitHub token); without it,
    the panel says GitHub is not connected. ``scout`` reads a workspace's repository on the local
    model and advises which agents it needs (with a database, a token and a model).
    ``summaries`` keeps each workspace's daily summary and the Board's briefings (with a database).
    ``on_start`` runs once the server is up, inside its event loop (the morning job starts there);
    ``on_close`` runs when the server stops (the laptop database's pool is closed there).

    Without ``chat`` the chat route answers ``chat.not_connected``. ``registry`` is
    what the Customize page shows (read once at start); without it the page is empty.
    With ``switches`` (a database) the page is read fresh each time and can be
    changed; without, the switch routes answer ``registry.read_only``.
    """
    page = registry or RegistryPage()
    log = get_logger("yaqzan.api")
    busy = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if on_start is not None:
            await on_start()
        try:
            yield
        finally:
            if on_close is not None:
                await on_close()

    app = FastAPI(title="Yaqzan", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    app.middleware("http")(_with_headers)

    directory = workspaces or WorkspaceDirectory(
        [
            WorkspaceRow(workspace_id=uuid5(_SAMPLE_NS, p.id), slug=p.id, name=p.name)
            for p in portfolio.projects()
        ]
    )

    app.state.workspaces = directory

    def detail_of(workspace: WorkspaceRow) -> ProjectDetail:
        # A project just added has no cards yet: its page says so ("No data yet"), never an error.
        return portfolio.project(workspace.slug) or ProjectDetail(project=_no_data(workspace), cards=())

    @app.get("/api/workspaces")
    def list_workspaces() -> tuple[ProjectSummary, ...]:
        listed = {p.id: p for p in portfolio.projects()}
        return tuple(listed.get(w.slug) or _no_data(w) for w in directory.all())

    @app.get("/api/workspaces/{slug}")
    def get_workspace(workspace: InWorkspace) -> ProjectDetail:
        return detail_of(workspace)

    @app.get("/api/models")
    def list_models() -> ModelList:
        return _connected(portfolio.models(), chat)

    _registry_routes(app, page, switches)
    _passkey_routes(app, switches)
    agent_routes(app, agents, _refuse_foreign)
    removal_routes(app, removal, directory, _refuse_foreign)
    adding_routes(app, adding, directory, _refuse_foreign)
    profile_routes(app, profiles)
    scout_routes(app, scout, _refuse_foreign)
    summary_routes(app, summaries, _refuse_foreign)

    async def answer(question: ChatQuestion, facts: str, sample: bool, workspace: str | None) -> ChatAnswer:
        if chat is None or question.model_id != chat.model_id:
            raise HTTPException(status_code=503, detail="chat.not_connected")
        if busy.locked():  # one question at a time: a laptop CPU runs one model answer at once
            raise HTTPException(status_code=429, detail="chat.busy")
        async with busy:
            try:
                reply = await chat.ask(question.question, facts, workspace)
            except SourceError as exc:
                say(log, "chat_failed", code=exc.reason_code, status=exc.status)
                raise HTTPException(status_code=502, detail=exc.reason_code) from None
        return ChatAnswer(
            model_id=chat.model_id, answer=reply.answer, from_sample=sample, used=reply.used[:8]
        )

    @app.post("/api/chat", dependencies=[Depends(_refuse_foreign)])
    async def ask_the_board(question: ChatQuestion) -> ChatAnswer:
        projects = tuple(listed for listed in portfolio.projects() if directory.find(listed.id) is not None)
        return await answer(question, portfolio_facts(projects), any(p.sample for p in projects), None)

    @app.post("/api/workspaces/{slug}/chat", dependencies=[Depends(_refuse_foreign)])
    async def ask_a_workspace(question: ChatQuestion, workspace: InWorkspace) -> ChatAnswer:
        detail = detail_of(workspace)
        return await answer(question, project_facts(detail), uses_sample(detail), workspace.slug)

    if static_dir is not None:
        if not (static_dir / "index.html").is_file():
            raise FileNotFoundError(f"no built web UI in {static_dir} (run the dashboard build first)")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    return app
