"""Expiry dates: when each certificate and domain ends (SDK 1.14.0, F11 slice 5b).

A certificate is read by a TLS handshake with the core's :class:`CertGetter`,
which only says whether the certificate is valid for that name and when it
ends: no page is ever asked for. A domain's end date comes from the public
registry (RDAP). Each name is checked on its own; one that could not be read
says so in its ``reason`` and never stops the others.

Reasons (``kind.reason``): ``cert.ok``, ``cert.expired``, ``cert.wrong_name``,
``cert.invalid``, ``cert.no_answer``; ``domain.ok``, ``domain.no_date``,
``domain.no_answer``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Lower-case labels, at least two; the last starts with a letter, so an IP address never passes.
HOST_NAME = r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]([a-z0-9-]*[a-z0-9])?$"
_REASON = r"^(cert|domain)\.[a-z_]+$"
MAX_NAMES = 100


class ExpiryKind(StrEnum):
    """What ends: a site's certificate or a domain's registration."""

    CERT = "cert"
    DOMAIN = "domain"


class ExpiryCheck(BaseModel):
    """One name's end date (``None`` when it could not be read or is not valid); ``reason`` says which."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ExpiryKind
    name: str = Field(pattern=HOST_NAME, max_length=253)
    expires_at: AwareDatetime | None = None
    reason: str = Field(pattern=_REASON)

    @model_validator(mode="after")
    def _reason_of_its_kind(self) -> ExpiryCheck:
        if not self.reason.startswith(f"{self.kind.value}."):
            raise ValueError("a check's reason belongs to its own kind")
        return self


class ExpiryReport(BaseModel):
    """Every name of one kind, read once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ExpiryKind
    checks: tuple[ExpiryCheck, ...] = Field(max_length=MAX_NAMES)
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def _one_kind(self) -> ExpiryReport:
        if any(check.kind is not self.kind for check in self.checks):
            raise ValueError("a report holds one kind of check")
        return self


@dataclass(frozen=True)
class CertAnswer:
    """What a handshake showed: the reason (``cert.ok`` or why not valid) and, when valid, the end date."""

    reason: str
    not_after: datetime | None = None


class CertGetter(Protocol):
    """One TLS handshake that reads only the certificate, given to a connector by the core."""

    async def certificate_of(self, host: str) -> CertAnswer:
        """The certificate's state. Raises :class:`~agentzaaheen.core.sdk.SourceError` if nothing answered."""
        ...
