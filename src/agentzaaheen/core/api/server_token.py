"""Saving the laptop's Cloudflare service token from the Customize page (ADR-033, F10 slice 4).

Both parts are pasted once. Saving answers ``server_token.needs_passkey`` until
Windows Hello signs that exact save (``/api/server-token/options``, then the
save again with the signed answer). Nothing here ever sends either part back:
the page is told only whether a token is saved and when.

Both parts are ``SecretStr`` with no limits of their own and an empty default, on
purpose: a refused or missing field would be echoed back in the error (a missing
one with the whole body). They are checked in the policy layer instead, which
answers ``server_token.bad_token`` and nothing more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr

from agentzaaheen.core.api.agents import StepUpOptions
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import ServerTokenStatus, ServerTokenVerdict
from agentzaaheen.core.store import StoreError


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TokenIn(_View):
    """The pasted parts (checked by the policy layer, never echoed).

    Both default to empty: a missing part must not fail validation, because that error
    would echo the whole body, and with it the other part.
    """

    client_id: SecretStr = SecretStr("")
    client_secret: SecretStr = SecretStr("")


class SignedTokenIn(TokenIn):
    """The pasted parts and Windows Hello's signed answer, once the save has asked for one."""

    credential: dict[str, Any] | None = None


class ServerTokenOut(_View):
    """What the page shows. ``switches``: saving is possible here (there is a database)."""

    switches: bool
    vault: bool
    saved: bool
    saved_at: datetime | None


class ServerTokens(Protocol):
    """What the routes need (``ServerTokenControl`` in real use)."""

    async def status(self) -> ServerTokenStatus:
        """Whether the vault is ready and a token is saved."""
        ...

    async def options(self, client_id: str, client_secret: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly this token."""
        ...

    async def save(
        self, client_id: str, client_secret: SecretStr, credential: dict[str, Any] | None
    ) -> ServerTokenVerdict:
        """Seal the token once this exact save is signed."""
        ...


def server_token_routes(
    app: FastAPI, tokens: ServerTokens | None, refuse_foreign: Callable[..., Any]
) -> None:
    """The routes for the server access token."""

    def ready() -> ServerTokens:
        if tokens is None:
            raise HTTPException(status_code=503, detail="server_token.read_only")
        return tokens

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError:
            raise HTTPException(status_code=503, detail="server_token.store_unavailable") from None
        except PasskeyError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None

    @app.get("/api/server-token")
    async def status() -> ServerTokenOut:
        if tokens is None:
            return ServerTokenOut(switches=False, vault=False, saved=False, saved_at=None)
        now = await stored(tokens.status())
        return ServerTokenOut(switches=True, vault=now.vault, saved=now.saved, saved_at=now.saved_at)

    @app.post("/api/server-token/options", dependencies=[Depends(refuse_foreign)])
    async def options(given: TokenIn) -> StepUpOptions:
        found = ready().options(given.client_id.get_secret_value(), given.client_secret)
        return StepUpOptions(options=await stored(found))

    @app.post("/api/server-token", dependencies=[Depends(refuse_foreign)])
    async def save(given: SignedTokenIn) -> ServerTokenOut:
        saving = ready().save(given.client_id.get_secret_value(), given.client_secret, given.credential)
        verdict = await stored(saving)
        if verdict is not ServerTokenVerdict.SAVED:
            raise HTTPException(status_code=409, detail=verdict.value)
        return await status()
