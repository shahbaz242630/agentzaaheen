"""The dashboard's web server: a read-only JSON API plus the built web UI.

Only GET routes exist. Every response carries a strict Content-Security-Policy
(SECURITY: no inline script, no remote images), and only the expected host
names are answered, which stops a hostile web page reaching this server
through DNS rebinding while it runs on the laptop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from yaqzan.core.api.views import ModelList, ProjectDetail, ProjectSummary

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


def create_app(
    portfolio: Portfolio,
    *,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
) -> FastAPI:
    """Build the app. ``static_dir`` is the built web UI; without it only the API is served."""
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
        return portfolio.models()

    if static_dir is not None:
        if not (static_dir / "index.html").is_file():
            raise FileNotFoundError(f"no built web UI in {static_dir} (run the dashboard build first)")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    return app
