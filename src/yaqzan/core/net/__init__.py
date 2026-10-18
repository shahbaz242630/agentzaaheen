"""The network: the one outbound HTTP client (Rulebook §4, SECURITY NET-1, §6.7).

What it does:
    :class:`JsonClient` makes read-only JSON GETs to one base url whose host
    is on an explicit allowlist. It refuses plain http, IP-literal hosts,
    credentials in urls, odd paths and redirects; caps response size; retries
    429/5xx/timeouts with jittered back-off that honours ``Retry-After``.

Public interface:
    ``JsonClient``, ``NetPolicy``, ``NetError``, ``NetErrorCode``,
    ``check_base_url``, ``check_path``. A ``JsonClient`` satisfies the SDK's
    ``JsonGetter`` protocol, which is all a connector ever sees.

How to test:
    ``uv run pytest tests/unit/test_net_client.py`` (no real network: every
    test runs through ``httpx.MockTransport``).

Must never:
    Send anything but GET; follow a redirect; talk to a host not on the
    allowlist; put a key in a url, an error or a log line. No other module
    may import an HTTP library (import-linter).
"""

from yaqzan.core.net.client import JsonClient, NetPolicy, check_base_url, check_path
from yaqzan.core.net.errors import NetError, NetErrorCode

__all__ = ["JsonClient", "NetError", "NetErrorCode", "NetPolicy", "check_base_url", "check_path"]
