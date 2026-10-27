"""Sealing chat words with the vault (F19 slice 1; Chairman, 27 Oct 2026: "lock the chats with the vault").

Each question and each answer is sealed on its own (a fresh data key each),
bound to its tenant, workspace, conversation, turn and side, so sealed text
copied to any other place will not open. The callers (the chat records door)
never see the vault: they get sealed bytes, or the words back, from here.

A failure (no master key, an altered or moved value) leaves as a
``SourceError`` carrying the vault's reason code, never a guess and never the
words.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import SecretStr, ValidationError

from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from agentzaaheen.core.vault import (
    CredentialSlot,
    Keyring,
    SealedSecret,
    VaultError,
    VaultErrorCode,
    open_secret,
    seal,
)

_CONNECTOR = "chat"  # the slot's kind for chat words (never a credential's)

Side = Literal["q", "a"]


class ChatSeal:
    """Seals and opens one tenant's chat turns. ``keyring`` is ``None`` when no master key file was found."""

    def __init__(self, tenant: UUID, keyring: Keyring | None) -> None:
        self._tenant = tenant
        self._keyring = keyring

    def _slot(self, workspace: UUID, conversation: UUID, turn: int, side: Side) -> CredentialSlot:
        return CredentialSlot(
            tenant_id=self._tenant,
            project_id=workspace,
            connector=_CONNECTOR,
            name=f"{side}_{conversation.hex}_{turn}",
            environment="production",
        )

    def _ring(self) -> Keyring:
        if self._keyring is None:
            raise _failed(
                VaultError(VaultErrorCode.BAD_KEY_FILE, "the vault's master key file was not found")
            )
        return self._keyring

    def seal_turn(
        self, workspace: UUID, conversation: UUID, turn: int, question: str, answer: str
    ) -> tuple[bytes, bytes]:
        """The sealed question and answer, as the vault's own JSON."""
        keyring = self._ring()
        try:
            return (
                seal(SecretStr(question), self._slot(workspace, conversation, turn, "q"), keyring)
                .model_dump_json()
                .encode("utf-8"),
                seal(SecretStr(answer), self._slot(workspace, conversation, turn, "a"), keyring)
                .model_dump_json()
                .encode("utf-8"),
            )
        except VaultError as exc:
            raise _failed(exc) from None

    def open_turn(
        self, workspace: UUID, conversation: UUID, turn: int, question: bytes, answer: bytes
    ) -> tuple[str, str]:
        """The words of a sealed turn; fails closed if either side was altered or moved."""
        keyring = self._ring()

        def opened(value: bytes, side: Side) -> str:
            try:
                sealed = SealedSecret.model_validate_json(value)
            except ValidationError:
                raise _failed(VaultError(VaultErrorCode.UNSUPPORTED, "not a sealed chat value")) from None
            slot = self._slot(workspace, conversation, turn, side)
            return open_secret(sealed, slot, keyring).get_secret_value()

        try:
            return opened(question, "q"), opened(answer, "a")
        except VaultError as exc:
            raise _failed(exc) from None


def _failed(exc: VaultError) -> SourceError:
    return SourceError(
        SourceErrorCode.AUTH, "a chat turn could not be sealed or opened", detail_code=exc.code
    )
