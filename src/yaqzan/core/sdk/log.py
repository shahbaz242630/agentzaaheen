"""The one logger: structured JSON lines with secrets and personal data removed.

Every log line passes through :func:`redact` before it is written, so a secret
that reaches a log call by mistake (a token in an error message, a password in a
dict) is replaced, never printed (SECURITY §9, threat CR-3).

Two layers:
    * by key name: values under keys such as ``password``, ``token`` or
      ``authorization`` are replaced (numbers and booleans are kept, so counts
      like ``tokens_in`` stay readable);
    * by pattern: every string is scanned for known secret shapes, emails and
      card numbers, wherever it appears (message, nested dicts, tracebacks).

No global configuration: :func:`get_logger` builds a fully redacting logger
each time, so there is no way to obtain an unredacted one by import order.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping
from typing import Any, TextIO

import structlog
from pydantic import SecretBytes, SecretStr

REDACTED = "[REDACTED]"
_MAX_DEPTH = 8
# Long strings are cut before scanning: logs are not for bulk content, and it keeps
# redaction fast on hostile input (a 200 KB blob from an untrusted source).
MAX_LOGGED_TEXT = 8_192
# Numeric values under these keys are counts, not secrets, so they stay readable.
_COUNT_KEY = re.compile(r"^(tokens?_(in|out|used)|.*_count|count_.*)$")

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "authorization",
    "auth_header",
    "cookie",
    "session",
    "private_key",
    "privatekey",
    "credential",
    "signature",
    "otp",
    "passkey",
    "master_key",
    "dek",
)

# (pattern, replacement). Order matters: whole blocks before their fragments.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
        REDACTED,
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), REDACTED),
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), REDACTED),
    (re.compile(r"\bwhsec_[A-Za-z0-9+/=]{10,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), REDACTED),
    (re.compile(r"(?<=://)[^/\s:@]{1,256}:[^/\s@]{1,256}@"), REDACTED + "@"),
    (
        re.compile(
            r"(?i)([?&](?:token|key|api_key|apikey|access_token|secret|password|sig|signature|code)=)[^&\s#]+"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}"),
        "[EMAIL]",
    ),
)

_CARD_CANDIDATE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"[ -]", "", match.group(0))
    return "[CARD]" if _luhn_ok(digits) else match.group(0)


def redact_text(text: str) -> str:
    """Return ``text`` with every known secret shape, email and card number replaced.

    Text longer than :data:`MAX_LOGGED_TEXT` is cut first and marked as cut.
    """
    if len(text) > MAX_LOGGED_TEXT:
        text = text[:MAX_LOGGED_TEXT] + f"...[CUT {len(text) - MAX_LOGGED_TEXT} chars]"
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _CARD_CANDIDATE.sub(_mask_card, text)


def contains_sensitive(text: str) -> bool:
    """Return True if :func:`redact_text` would change ``text``."""
    return redact_text(text) != text


def is_sensitive_key(key: str) -> bool:
    """Return True if a value stored under ``key`` must never be logged."""
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _keep_under_sensitive_key(key: str, value: object) -> bool:
    if value is None or isinstance(value, bool):
        return True
    return isinstance(value, int | float) and bool(_COUNT_KEY.match(key.lower()))


def redact(value: object, *, _depth: int = 0) -> object:  # noqa: PLR0911  # one return per kind of value
    """Return a copy of ``value`` that is safe to log. Never mutates the input."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, SecretStr | SecretBytes):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {redact_text(str(k)): _redact_item(str(k), v, _depth) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item, _depth=_depth + 1) for item in value]
    return redact_text(repr(value))


def _redact_item(key: str, value: object, depth: int) -> object:
    if is_sensitive_key(key) and not _keep_under_sensitive_key(key, value):
        return REDACTED
    return redact(value, _depth=depth + 1)


def _redact_processor(_logger: object, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {redact_text(str(key)): _redact_item(str(key), value, 0) for key, value in event_dict.items()}


def get_logger(name: str, *, sink: TextIO | None = None, **context: object) -> Any:  # noqa: ANN401
    """Return a logger that writes redacted JSON lines to ``sink`` (stderr by default).

    ``context`` values are bound to every line, e.g. ``correlation_id``.
    The return type is structlog's bound logger (``.info(...)``, ``.bind(...)``).
    """
    processors: list[Callable[..., Any]] = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        _redact_processor,
        structlog.processors.JSONRenderer(sort_keys=True),
    ]
    logger = structlog.wrap_logger(
        structlog.WriteLogger(sink if sink is not None else sys.stderr),
        processors=processors,
        wrapper_class=structlog.BoundLogger,
        cache_logger_on_first_use=False,
    )
    return logger.bind(component=name, **context)
