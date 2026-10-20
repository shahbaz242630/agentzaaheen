"""The dashboard's web server: a read-only JSON API plus the built web UI.

Only GET routes exist, plus a few POSTs: the chat (F05, ADR-024), which reads
the cards and asks a model and changes nothing, and the Customize page's
switches (ADR-030), which can only switch an item off, ask for it to be
switched on, or withdraw that request. Every POST answers only same-origin
JSON requests that carry the ``X-Yaqzan-Request`` header, so another web page
cannot use it. Every response carries a strict Content-Security-Policy
(SECURITY: no inline script, no remote images), and only the expected host
names are answered, which stops a hostile web page reaching this server
through DNS rebinding while it runs on the laptop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from yaqzan.core.api.facts import portfolio_facts, project_facts, uses_sample
from yaqzan.core.api.registry import ItemRef, RegistryPage
from yaqzan.core.api.views import ChatAnswer, ChatQuestion, ModelList, ProjectDetail, ProjectSummary
from yaqzan.core.llm_gateway import ChatReply
from yaqzan.core.policy import Verdict
from yaqzan.core.registry import Kind
from yaqzan.core.sdk import SourceError, get_logger
from yaqzan.core.store import StoreError

LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")

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

    async def ask(self, question: str, facts: str) -> ChatReply:
        """The answer to ``question``, from ``facts`` and read-only tools, and what it used."""
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


REQUEST_HEADER = "X-Yaqzan-Request"


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


def _registry_routes(app: FastAPI, page: RegistryPage, switches: RegistrySwitches | None) -> None:
    """The Customize page: read it, and (with a database) switch off, ask, withdraw (ADR-030)."""
    log = get_logger("yaqzan.api")

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError as exc:
            log.warning("registry_store_failed", code=exc.code.value)
            raise HTTPException(status_code=503, detail="registry.store_unavailable") from None

    async def change(act: Callable[[RegistrySwitches], Awaitable[Verdict]]) -> RegistryPage:
        if switches is None:
            raise HTTPException(status_code=503, detail="registry.read_only")
        verdict = await stored(act(switches))
        if verdict is not Verdict.DONE:
            raise HTTPException(status_code=409, detail=verdict.value)
        return await stored(switches.page())

    @app.get("/api/registry")
    async def get_registry() -> RegistryPage:
        return page if switches is None else await stored(switches.page())

    @app.post("/api/registry/switch-off", dependencies=[Depends(_refuse_foreign)])
    async def switch_off(item: ItemRef) -> RegistryPage:
        return await change(lambda s: s.switch_off(item.kind, item.id))

    @app.post("/api/registry/requests", dependencies=[Depends(_refuse_foreign)])
    async def request_on(item: ItemRef) -> RegistryPage:
        return await change(lambda s: s.request_on(item.kind, item.id))

    @app.post("/api/registry/requests/{request_id}/withdraw", dependencies=[Depends(_refuse_foreign)])
    async def withdraw(request_id: UUID) -> RegistryPage:
        return await change(lambda s: s.withdraw(request_id))


def create_app(  # noqa: PLR0913  # the portfolio, then each optional part of the dashboard
    portfolio: Portfolio,
    *,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
    chat: ChatBackend | None = None,
    registry: RegistryPage | None = None,
    switches: RegistrySwitches | None = None,
) -> FastAPI:
    """Build the app. ``static_dir`` is the built web UI; without it only the API is served.

    Without ``chat`` the chat route answers ``chat.not_connected``. ``registry`` is
    what the Customize page shows (read once at start); without it the page is empty.
    With ``switches`` (a database) the page is read fresh each time and can be
    changed; without, the switch routes answer ``registry.read_only``.
    """
    page = registry or RegistryPage()
    log = get_logger("yaqzan.api")
    busy = asyncio.Lock()
    app = FastAPI(title="Yaqzan", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/projects")
    def list_projects() -> tuple[ProjectSummary, ...]:
        return portfolio.projects()

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> ProjectDetail:
        detail = portfolio.project(project_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="project.not_found")
        return detail

    @app.get("/api/models")
    def list_models() -> ModelList:
        return _connected(portfolio.models(), chat)

    _registry_routes(app, page, switches)

    @app.post("/api/chat", dependencies=[Depends(_refuse_foreign)])
    async def ask(question: ChatQuestion) -> ChatAnswer:
        if chat is None or question.model_id != chat.model_id:
            raise HTTPException(status_code=503, detail="chat.not_connected")
        if question.project_id is None:
            projects = portfolio.projects()
            facts, sample = portfolio_facts(projects), any(p.sample for p in projects)
        else:
            detail = portfolio.project(question.project_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="project.not_found")
            facts, sample = project_facts(detail), uses_sample(detail)
        if busy.locked():  # one question at a time: a laptop CPU runs one model answer at once
            raise HTTPException(status_code=429, detail="chat.busy")
        async with busy:
            try:
                reply = await chat.ask(question.question, facts)
            except SourceError as exc:
                log.warning("chat_failed", code=exc.reason_code, status=exc.status)
                raise HTTPException(status_code=502, detail=exc.reason_code) from None
        return ChatAnswer(
            model_id=chat.model_id, answer=reply.answer, from_sample=sample, used=reply.used[:8]
        )

    if static_dir is not None:
        if not (static_dir / "index.html").is_file():
            raise FileNotFoundError(f"no built web UI in {static_dir} (run the dashboard build first)")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    return app
