"""Saving our server model's key from the Customize page (F10 slice 6).

The key is pasted once. Saving answers ``model_key.needs_passkey`` until Windows
Hello signs that exact save (``/api/model-key/options``, then the save again
with the signed answer). Nothing here ever sends the key back: the page is told
only whether a key is saved and when.

The body's ``key`` is a ``SecretStr`` with no limits of its own, on purpose: a
refused field would be echoed back in the error. It is checked in the policy
layer instead, which answers ``model_key.bad_key`` and nothing more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException
from pydantic import SecretStr

from agentzaaheen.core.api.agents import StepUpOptions
from agentzaaheen.core.api.claude_key import ClaudeKeyOut, KeyIn, SignedKeyIn
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import ModelKeyStatus, ModelKeyVerdict
from agentzaaheen.core.store import StoreError


class ModelKeys(Protocol):
    """What the routes need (``ModelKeyControl`` in real use)."""

    async def status(self) -> ModelKeyStatus:
        """Whether the vault is ready and a key is saved."""
        ...

    async def options(self, key: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly ``key``."""
        ...

    async def save(self, key: SecretStr, credential: dict[str, Any] | None) -> ModelKeyVerdict:
        """Seal the key once this exact save is signed."""
        ...


def model_key_routes(app: FastAPI, keys: ModelKeys | None, refuse_foreign: Callable[..., Any]) -> None:
    """The routes for the server model's key (the same shapes as the Claude key's)."""

    def ready() -> ModelKeys:
        if keys is None:
            raise HTTPException(status_code=503, detail="model_key.read_only")
        return keys

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError:
            raise HTTPException(status_code=503, detail="model_key.store_unavailable") from None
        except PasskeyError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None

    @app.get("/api/model-key")
    async def status() -> ClaudeKeyOut:
        if keys is None:
            return ClaudeKeyOut(switches=False, vault=False, saved=False, saved_at=None)
        now = await stored(keys.status())
        return ClaudeKeyOut(switches=True, vault=now.vault, saved=now.saved, saved_at=now.saved_at)

    @app.post("/api/model-key/options", dependencies=[Depends(refuse_foreign)])
    async def options(given: KeyIn) -> StepUpOptions:
        return StepUpOptions(options=await stored(ready().options(given.key)))

    @app.post("/api/model-key", dependencies=[Depends(refuse_foreign)])
    async def save(given: SignedKeyIn) -> ClaudeKeyOut:
        verdict = await stored(ready().save(given.key, given.credential))
        if verdict is not ModelKeyVerdict.SAVED:
            raise HTTPException(status_code=409, detail=verdict.value)
        return await status()
