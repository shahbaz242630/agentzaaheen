"""Network errors: a reason code and the HTTP status, never a body, url query or header.

A :class:`NetError` is also the SDK's ``SourceError``, so a connector that
only knows the SDK can handle it without importing this module.
"""

from __future__ import annotations

from enum import StrEnum

from yaqzan.core.sdk import SourceError, SourceErrorCode


class NetErrorCode(StrEnum):
    """Why an outbound call failed. Safe to log, store and show."""

    BLOCKED = "net.blocked"
    TIMEOUT = "net.timeout"
    UNAVAILABLE = "net.unavailable"
    AUTH = "net.auth"
    RATE_LIMITED = "net.rate_limited"
    SERVER_ERROR = "net.server_error"
    CLIENT_ERROR = "net.client_error"
    TOO_LARGE = "net.too_large"
    BAD_RESPONSE = "net.bad_response"


_AS_SOURCE: dict[NetErrorCode, SourceErrorCode] = {
    NetErrorCode.BLOCKED: SourceErrorCode.REFUSED,
    NetErrorCode.TIMEOUT: SourceErrorCode.UNAVAILABLE,
    NetErrorCode.UNAVAILABLE: SourceErrorCode.UNAVAILABLE,
    NetErrorCode.AUTH: SourceErrorCode.AUTH,
    NetErrorCode.RATE_LIMITED: SourceErrorCode.RATE_LIMITED,
    NetErrorCode.SERVER_ERROR: SourceErrorCode.UNAVAILABLE,
    NetErrorCode.CLIENT_ERROR: SourceErrorCode.REFUSED,
    NetErrorCode.TOO_LARGE: SourceErrorCode.TOO_MUCH_DATA,
    NetErrorCode.BAD_RESPONSE: SourceErrorCode.BAD_RESPONSE,
}


class NetError(SourceError):
    """An outbound call failed, or was refused before it was sent.

    ``net_code`` is the precise network reason; ``code`` is the SDK reason a
    connector sees.
    """

    def __init__(self, net_code: NetErrorCode, message: str, *, status: int | None = None) -> None:
        super().__init__(_AS_SOURCE[net_code], message, detail_code=net_code.value, status=status)
        suffix = f" (http {status})" if status is not None else ""
        self.args = (f"{net_code.value}: {message}{suffix}",)
        self.net_code = net_code
