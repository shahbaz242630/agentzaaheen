"""The dashboard's web server: a read-only JSON API plus the built web UI.

Only GET routes exist, plus one POST: the chat (F05, ADR-024), which reads the
cards and asks a model, and changes nothing. It answers only same-origin
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

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from yaqzan.core.api.facts import portfolio_facts, project_facts, uses_sample
from yaqzan.core.api.registry import RegistryPage
from yaqzan.core.api.views import ChatAnswer, ChatQuestion, ModelList, ProjectDetail, ProjectSummary
from yaqzan.core.llm_gateway import ChatReply
from yaqzan.core.sdk import SourceError, get_logger

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


def create_app(
    portfolio: Portfolio,
    *,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
    chat: ChatBackend | None = None,
    registry: RegistryPage | None = None,
) -> FastAPI:
    """Build the app. ``static_dir`` is the built web UI; without it only the API is served.

    Without ``chat`` the chat route answers ``chat.not_connected``. ``registry`` is
    what the Customize page shows (read once at start); without it the page is empty.
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

    @app.get("/api/registry")
    def get_registry() -> RegistryPage:
        return page

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
