"""The network: the one outbound HTTP client (Rulebook §4, SECURITY NET-1, §6.7).

What it does:
    :class:`JsonClient` makes read-only JSON GETs to one base url whose host
    is on an explicit allowlist. It refuses plain http, IP-literal hosts,
    credentials in urls, odd paths and redirects; caps response size; retries
    429/5xx/timeouts with jittered back-off that honours ``Retry-After``.
    :class:`ModelClient` makes the core's only POST: one chat request to an
    OpenAI-compatible model server, on 127.0.0.1 or an allowlisted https host
    (ADR-024). It is never retried. :meth:`JsonClient.get_redirected_bytes`
    follows exactly one redirect to a pre-signed download link whose host
    matches the client's :class:`DownloadPolicy` (ADR-029), sending it no
    credentials.

Public interface:
    ``JsonClient``, ``NetPolicy``, ``DownloadPolicy``, ``NetError``, ``NetErrorCode``,
    ``check_base_url``, ``check_path``, ``ModelClient``, ``check_model_url``.
    A ``JsonClient`` satisfies the SDK's ``JsonGetter`` protocol, which is all
    a connector ever sees.

How to test:
    ``uv run pytest tests/unit/test_net_client.py tests/unit/test_net_model.py`` (no real network: every
    test runs through ``httpx.MockTransport``).

Must never:
    Send anything but GET (the model chat POST aside); follow a redirect (the one signed
    download aside, with no credentials); talk to a host not on the
    allowlist; put a key in a url, an error or a log line. No other module
    may import an HTTP library (import-linter).
"""

from yaqzan.core.net.client import DownloadPolicy, JsonClient, NetPolicy, check_base_url, check_path
from yaqzan.core.net.errors import NetError, NetErrorCode
from yaqzan.core.net.model import ModelClient, check_model_url

__all__ = [
    "DownloadPolicy",
    "JsonClient",
    "ModelClient",
    "NetError",
    "NetErrorCode",
    "NetPolicy",
    "check_base_url",
    "check_model_url",
    "check_path",
]
