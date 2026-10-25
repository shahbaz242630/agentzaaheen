"""A failed build's log: a small, cleaned, redacted piece of it, always untrusted (ADR-029).

Logs are text anyone who can push code can write, so a log is never a fact
and never an instruction: a model sees it only wrapped as untrusted data.
Only the part around the first error is kept (at most :data:`MAX_LOG_LINES`
lines and :data:`MAX_LOG_CHARS` characters), with timestamps and colour codes
removed and every line passed through the redactor (keys, tokens, emails,
card numbers). The digest lets the audit log name the exact text without
storing it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agentzaaheen.core.sdk.code import REPO_PATTERN
from agentzaaheen.core.sdk.log import redact_text

MAX_LOG_LINES = 200
MAX_LOG_CHARS = 16_000
MAX_LINE_CHARS = 400
_ERROR_MARK = "##[error]"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class BytesGetter(Protocol):
    """A read-only GET that follows one redirect to a signed download link (the core's client)."""

    async def get_redirected_bytes(self, path: str, params: Mapping[str, str]) -> bytes:
        """The downloaded bytes. Raises :class:`~agentzaaheen.core.sdk.SourceError` (or a subclass) only."""
        ...


class FailedStepLog(BaseModel):
    """The cleaned tail of one failed job's log. ``job`` and ``step`` are written by people: untrusted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    repo: str = Field(pattern=REPO_PATTERN)
    run_id: int = Field(ge=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    job: str = Field(min_length=1, max_length=200)
    step: str | None = Field(default=None, max_length=200)
    lines: tuple[str, ...] = Field(max_length=MAX_LOG_LINES)
    truncated: bool
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    finished_at: AwareDatetime | None = None
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def _small_and_named(self) -> FailedStepLog:
        if sum(len(line) for line in self.lines) > MAX_LOG_CHARS:
            raise ValueError(f"a log excerpt is at most {MAX_LOG_CHARS} characters")
        if any(len(line) > MAX_LINE_CHARS for line in self.lines):
            raise ValueError(f"a log line is at most {MAX_LINE_CHARS} characters")
        if self.digest != log_digest(self.lines):
            raise ValueError("the digest does not match the lines")
        return self


def log_digest(lines: tuple[str, ...]) -> str:
    """SHA-256 of the excerpt as it is shown (lines joined by newlines)."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def clean_log(raw: bytes) -> tuple[tuple[str, ...], bool]:
    """The excerpt to keep from a raw log, and whether anything was left out.

    It ends at the first ``##[error]`` line (or the end), keeps at most the
    :data:`MAX_LOG_LINES` lines before it, then trims from the front until the
    whole excerpt fits :data:`MAX_LOG_CHARS`.
    """
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_line(line) for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    end = next((i for i, line in enumerate(lines) if _ERROR_MARK in line), len(lines) - 1)
    start = max(0, end - MAX_LOG_LINES + 1)
    kept = lines[start : end + 1]
    truncated = start > 0 or end < len(lines) - 1
    while kept and sum(len(line) for line in kept) > MAX_LOG_CHARS:
        kept.pop(0)
        truncated = True
    return tuple(kept), truncated


def _clean_line(line: str) -> str:
    plain = _CONTROL.sub("", _ANSI.sub("", _TIMESTAMP.sub("", line))).rstrip()
    shown = redact_text(plain)
    return shown if len(shown) <= MAX_LINE_CHARS else shown[: MAX_LINE_CHARS - 1] + "…"


def make_failed_step_log(  # noqa: PLR0913  # one field each, all checked by the shape
    *,
    source: str,
    repo: str,
    run_id: int,
    commit: str,
    job: str,
    step: str | None,
    raw: bytes,
    finished_at: datetime | None,
    fetched_at: datetime,
) -> FailedStepLog:
    """The shape from a raw download: cleaned, redacted, digested. Names are redacted and cut too."""
    lines, truncated = clean_log(raw)
    return FailedStepLog(
        source=source,
        repo=repo,
        run_id=run_id,
        commit=commit,
        job=_name(job) or "unnamed job",
        step=_name(step) if step else None,
        lines=lines,
        truncated=truncated,
        digest=log_digest(lines),
        finished_at=finished_at,
        fetched_at=fetched_at,
    )


def _name(text: str | None) -> str:
    return redact_text(_CONTROL.sub("", text or "").strip())[:200]


class BuildLogSource(Protocol):
    """What a code-hosting connector offers for triage: the newest failed build's log, read on demand."""

    @property
    def repo(self) -> str:
        """The repository it reads, ``owner/name``."""
        ...

    async def fetch(self, *, now: datetime) -> FailedStepLog | None:
        """The newest failed run's first failed job, or ``None`` if the main branch has none.

        Raises :class:`~agentzaaheen.core.sdk.SourceError` only.
        """
        ...
