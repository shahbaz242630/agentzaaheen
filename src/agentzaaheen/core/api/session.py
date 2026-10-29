"""Signing in to the dashboard and out again (ADR-SEC-010, F19 slice 2).

With a :class:`SessionGate` every ``/api`` route needs a live session, except
the few in :data:`OPEN_ROUTES` (passkey status and set-up, and signing in and
out). The page files are not routes, so the sign-in screen can always load.
Signing in is one Windows Hello touch; the session lives in a cookie that holds
a random token, HttpOnly and SameSite=Strict, kept here only as a hash.

A call counts as a person's action, and so restarts the 3 idle hours, only
when the page marks it with ``X-AgentZaaheen-Active: 1``; anything automatic
leaves the mark off, so an open page never keeps itself signed in.

Without a gate (our server's copy, tests) nothing is asked and
``GET /api/session`` says sign-in is not needed here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from agentzaaheen.core.api.agents import StepUpOptions
from agentzaaheen.core.auth import LONGEST, PasskeyError, SessionCheck, Sessions, SignInWait
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import StoreError

COOKIE = "az_session"
ACTIVE_HEADER = "X-AgentZaaheen-Active"
# (method, route template) that answer without a session: everything needed to get one.
OPEN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/session"),
        ("POST", "/api/session/options"),
        ("POST", "/api/session"),
        ("POST", "/api/session/sign-out"),
        ("GET", "/api/passkey"),
        ("POST", "/api/passkey/enrol/options"),
        ("POST", "/api/passkey/enrol"),
    }
)
BY_BUTTON = "session.by_button"
REPLACED = "session.replaced"


class SignIns(Protocol):
    """The passkey checks sign-in needs (``PasskeyApprovals`` through the laptop's switches in real use)."""

    async def sign_in_options(self) -> dict[str, Any]:
        """Options for Windows Hello to sign in."""
        ...

    async def sign_in(self, credential: dict[str, Any]) -> bool:
        """Whether the signed answer holds (audited)."""
        ...

    async def signed_out(self, reason: str) -> None:
        """Audit a session's end."""
        ...


class SessionOut(BaseModel):
    """What the page is told: whether sign-in is needed here, and whether this browser is signed in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required: bool
    signed_in: bool


class SignInAnswer(BaseModel):
    """Windows Hello's signed answer to the sign-in challenge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    credential: dict[str, Any]


class SessionGate:
    """Who is signed in, and the check every closed route runs first."""

    def __init__(self, sign_ins: SignIns, clock: Callable[[], datetime]) -> None:
        self._sign_ins = sign_ins
        self._sessions = Sessions(clock)
        self._wait = SignInWait(clock)

    async def require(self, request: Request) -> None:
        """Let the request through only with a live session (or to an open route); else 401."""
        route = getattr(request.scope.get("route"), "path", None)
        if (request.method, route) in OPEN_ROUTES:
            return
        active = request.headers.get(ACTIVE_HEADER) == "1"
        checked = self._sessions.check(request.cookies.get(COOKIE), active=active)
        if checked is SessionCheck.LIVE:
            return
        if checked is not SessionCheck.NONE:
            await self._ended(checked.value)
        raise HTTPException(status_code=401, detail="session.needed")

    def signed_in(self, request: Request) -> bool:
        """Whether this request carries a live session (does not count as a person's action)."""
        return self._sessions.check(request.cookies.get(COOKIE), active=False) is SessionCheck.LIVE

    async def options(self) -> dict[str, Any]:
        """Options for the sign-in signature, unless sign-in must wait."""
        if self._wait.waiting():
            raise HTTPException(status_code=429, detail="session.wait")
        try:
            return await self._sign_ins.sign_in_options()
        except PasskeyError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None
        except StoreError:
            raise HTTPException(status_code=503, detail="session.store_unavailable") from None

    async def sign_in(self, request: Request, response: Response, credential: dict[str, Any]) -> None:
        """Check the signed answer; if it holds, start a session and set its cookie. Else 403 (or 429)."""
        if self._wait.waiting():
            raise HTTPException(status_code=429, detail="session.wait")
        try:
            good = await self._sign_ins.sign_in(credential)
        except StoreError:
            raise HTTPException(status_code=503, detail="session.store_unavailable") from None
        if not good:
            log = get_logger("agentzaaheen.session")
            say(log, "sign_in_failed")
            if self._wait.failed():
                say(log, "sign_in_waiting")
            raise HTTPException(status_code=403, detail="session.sign_in_failed")
        self._wait.succeeded()
        await self.start(request, response)

    async def start(self, request: Request, response: Response) -> None:
        """Start a session for a Chairman just proven by Windows Hello (sign-in or a new passkey)."""
        self.end_cookie(request)  # a browser holds one session at most
        started = self._sessions.start()
        say(get_logger("agentzaaheen.session"), "session_started")
        if started.ended_oldest:
            await self._ended(REPLACED)
        response.set_cookie(
            COOKIE,
            started.token,
            max_age=int(LONGEST.total_seconds()),
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )

    def end_cookie(self, request: Request) -> bool:
        """End the session this request carries, without auditing; whether one was live."""
        return self._sessions.end(request.cookies.get(COOKIE))

    async def sign_out(self, request: Request, response: Response) -> None:
        """End this browser's session (audited if one was live) and clear its cookie."""
        if self.end_cookie(request):
            await self._ended(BY_BUTTON)
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")

    async def _ended(self, reason: str) -> None:
        log = get_logger("agentzaaheen.session")
        say(log, "session_ended", reason=reason)
        try:
            await self._sign_ins.signed_out(reason)
        except StoreError as exc:  # the line above already says it ended; the audit row is what failed
            say(log, "session_audit_failed", code=exc.code.value)


def session_routes(app: FastAPI, gate: SessionGate | None, refuse_foreign: Callable[..., Any]) -> None:
    """Signing in and out, and whether this browser is signed in."""

    def ready() -> SessionGate:
        if gate is None:
            raise HTTPException(status_code=404, detail="session.not_needed")
        return gate

    @app.get("/api/session")
    async def status(request: Request) -> SessionOut:
        if gate is None:
            return SessionOut(required=False, signed_in=False)
        return SessionOut(required=True, signed_in=gate.signed_in(request))

    @app.post("/api/session/options", dependencies=[Depends(refuse_foreign)])
    async def options() -> StepUpOptions:
        return StepUpOptions(options=await ready().options())

    @app.post("/api/session", dependencies=[Depends(refuse_foreign)])
    async def sign_in(answer: SignInAnswer, request: Request, response: Response) -> SessionOut:
        await ready().sign_in(request, response, answer.credential)
        return SessionOut(required=True, signed_in=True)

    @app.post("/api/session/sign-out", dependencies=[Depends(refuse_foreign)])
    async def sign_out(request: Request, response: Response) -> SessionOut:
        await ready().sign_out(request, response)
        return SessionOut(required=True, signed_in=False)
