"""Saving the Claude API key from the Customize page (ADR-033, F10 slice 2c).

The key is pasted once. Saving answers ``claude_key.needs_passkey`` until
Windows Hello signs that exact save (``/api/claude-key/options``, then the save
again with the signed answer). Nothing here ever sends the key back: the page
is told only whether a key is saved and when.

The body's ``key`` is a ``SecretStr`` with no limits of its own, on purpose: a
refused field would be echoed back in the error. It is checked in the policy
layer instead, which answers ``claude_key.bad_key`` and nothing more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr

from agentzaaheen.core.api.agents import StepUpOptions
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import ClaudeKeyStatus, ClaudeKeyVerdict
from agentzaaheen.core.store import StoreError


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class KeyIn(_View):
    """The pasted key (checked by the policy layer, never echoed)."""

    key: SecretStr


class SignedKeyIn(KeyIn):
    """The pasted key and Windows Hello's signed answer, once the save has asked for one."""

    credential: dict[str, Any] | None = None


class ClaudeKeyOut(_View):
    """What the page shows. ``switches``: saving is possible here (there is a database)."""

    switches: bool
    vault: bool
    saved: bool
    saved_at: datetime | None


class ClaudeKeys(Protocol):
    """What the routes need (``ClaudeKeyControl`` in real use)."""

    async def status(self) -> ClaudeKeyStatus:
        """Whether the vault is ready and a key is saved."""
        ...

    async def options(self, key: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly ``key``."""
        ...

    async def save(self, key: SecretStr, credential: dict[str, Any] | None) -> ClaudeKeyVerdict:
        """Seal the key once this exact save is signed."""
        ...


def claude_key_routes(app: FastAPI, keys: ClaudeKeys | None, refuse_foreign: Callable[..., Any]) -> None:
    """The routes for the Claude key."""

    def ready() -> ClaudeKeys:
        if keys is None:
            raise HTTPException(status_code=503, detail="claude_key.read_only")
        return keys

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError:
            raise HTTPException(status_code=503, detail="claude_key.store_unavailable") from None
        except PasskeyError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None

    @app.get("/api/claude-key")
    async def status() -> ClaudeKeyOut:
        if keys is None:
            return ClaudeKeyOut(switches=False, vault=False, saved=False, saved_at=None)
        now = await stored(keys.status())
        return ClaudeKeyOut(switches=True, vault=now.vault, saved=now.saved, saved_at=now.saved_at)

    @app.post("/api/claude-key/options", dependencies=[Depends(refuse_foreign)])
    async def options(given: KeyIn) -> StepUpOptions:
        return StepUpOptions(options=await stored(ready().options(given.key)))

    @app.post("/api/claude-key", dependencies=[Depends(refuse_foreign)])
    async def save(given: SignedKeyIn) -> ClaudeKeyOut:
        verdict = await stored(ready().save(given.key, given.credential))
        if verdict is not ClaudeKeyVerdict.SAVED:
            raise HTTPException(status_code=409, detail=verdict.value)
        return await status()
