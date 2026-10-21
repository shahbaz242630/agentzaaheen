"""Uptime: whether each of a project's public addresses answers as it should (SDK 1.7.0, F03 slice 4).

A connector only ever sees an address's **status code and timing**, through
the core's :class:`StatusGetter`; never the page itself (a page is written by
whoever controls the server, so it is untrusted and is not read at all).

Each address has a rule:

- ``ok``: it must answer with a page or a redirect (2xx or 3xx). For websites
  and health pages.
- ``answer``: it must answer at all without a server error (anything below
  500). For a bare API address that has no page of its own.

An address may also name its **part** of the product (``site``, ``api``,
``sign-in``; SDK 1.9.0), so an alert and the fix guides can say which part is
down rather than "1 of 6 addresses". A part is our own short name, never an
address.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

URL = r"^https://[a-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$"
_REASON = r"^uptime\.[a-z_]+$"
PART = r"^[a-z0-9]+(-[a-z0-9]+)*$"
MAX_PART = 24
MAX_SITES = 50
_SERVER_ERROR = 500
_REDIRECT = range(300, 400)
_OK = range(200, 300)
_NOT_FOUND = 404


class Expect(StrEnum):
    """What counts as up for one address."""

    OK = "ok"  # a page or a redirect (2xx, 3xx)
    ANSWER = "answer"  # any answer that is not a server error (< 500)


class SiteRule(BaseModel):
    """One address to check, and its rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(pattern=URL, max_length=300)
    expect: Expect = Expect.OK
    part: str | None = Field(default=None, pattern=PART, max_length=MAX_PART)

    @field_validator("url")
    @classmethod
    def _a_name_not_an_ip(cls, url: str) -> str:
        try:
            ipaddress.ip_address(urlsplit(url).hostname or "")
        except ValueError:
            return url
        raise ValueError("use the address's name, not an IP address")


@dataclass(frozen=True)
class StatusAnswer:
    """What an address answered: its status code and how long the answer took."""

    status: int
    elapsed_ms: int


class StatusGetter(Protocol):
    """One GET that reads only the status line, given to a connector by the core (the one HTTP client)."""

    async def status_of(self, url: str) -> StatusAnswer:
        """The status and timing. Raises :class:`~yaqzan.core.sdk.SourceError` (or a subclass) only."""
        ...


class SiteCheck(BaseModel):
    """How one address stood when it was checked. ``reason`` says why in a code (``uptime.*``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(pattern=URL, max_length=300)
    expect: Expect
    part: str | None = Field(default=None, pattern=PART, max_length=MAX_PART)
    up: bool
    status: int | None = Field(default=None, ge=100, le=599)
    latency_ms: int | None = Field(default=None, ge=0)
    reason: str = Field(pattern=_REASON)
    checked_at: AwareDatetime


class UptimeReport(BaseModel):
    """Every address of one project, checked once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = "uptime"
    sites: tuple[SiteCheck, ...] = Field(min_length=1, max_length=MAX_SITES)
    fetched_at: AwareDatetime

    @property
    def up(self) -> int:
        """How many addresses are up."""
        return sum(site.up for site in self.sites)


def judge(rule: SiteRule, status: int) -> tuple[bool, str]:
    """Whether ``status`` counts as up under ``rule``, and the reason code."""
    if status >= _SERVER_ERROR:
        return False, "uptime.server_error"
    if status in _OK:
        return True, "uptime.ok"
    if status in _REDIRECT:
        return True, "uptime.redirect"
    if rule.expect is Expect.ANSWER:
        return True, "uptime.answered"
    return False, "uptime.not_found" if status == _NOT_FOUND else "uptime.refused"


def check_site(rule: SiteRule, answer: StatusAnswer | None, checked_at: datetime) -> SiteCheck:
    """The check for one address; ``answer`` is None when it did not answer at all."""
    if answer is None:
        return SiteCheck(
            url=rule.url,
            expect=rule.expect,
            part=rule.part,
            up=False,
            reason="uptime.no_answer",
            checked_at=checked_at,
        )
    up, reason = judge(rule, answer.status)
    return SiteCheck(
        url=rule.url,
        expect=rule.expect,
        part=rule.part,
        up=up,
        status=answer.status,
        latency_ms=answer.elapsed_ms,
        reason=reason,
        checked_at=checked_at,
    )
