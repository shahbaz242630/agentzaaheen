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
    credentials. :class:`StatusProbe` makes status-only GETs for uptime: it
    reads the status line and closes, never the page, never a redirect.
    :class:`PushClient` posts one short alert to the Chairman's secret ntfy
    topic (ADR-031), the core's only outbound message. :class:`OtlpSender`
    posts finished spans to the trace store's OTLP gateway (ADR-032), never
    retried, dropped on failure. :class:`HeartbeatClient` makes one GET to a
    Healthchecks ping address, so a job that stops running raises an alert.
    :class:`ClaudeClient` is the one door to Claude (ADR-033): one Messages
    POST to ``api.anthropic.com`` only, pinned model ids, the key asked for
    once per call, never retried, size-capped. :class:`CertProbe` reads a
    certificate with one TLS handshake to an allowlisted name (whether it is
    valid, and when it ends), and sends nothing after it. :func:`list_tools`
    tests a custom MCP server the Chairman added (ADR-041): handshake and tool
    list only, public https addresses only, 8 s at most, never a tool call.

Public interface:
    ``JsonClient``, ``NetPolicy``, ``DownloadPolicy``, ``NetError``, ``NetErrorCode``,
    ``check_base_url``, ``check_path``, ``check_read_query``, ``ModelClient``, ``check_model_url``,
    ``StatusProbe``, ``check_probe_url``, ``PushClient``, ``NTFY_HOST``,
    ``OtlpSender``, ``check_otlp_endpoint``, ``HeartbeatClient``,
    ``HEALTHCHECKS_PING_HOST``, ``check_ping_url``, ``ClaudeClient``, ``ClaudeReply``,
    ``CLAUDE_HOST``, ``CertProbe``, ``list_tools``, ``check_mcp_url``, ``McpTool``, ``McpListing``.
    A ``JsonClient`` satisfies the SDK's ``JsonGetter`` protocol, which is all
    a connector ever sees.

How to test:
    ``uv run pytest tests/unit/test_net_client.py tests/unit/test_net_model.py`` (no real network: every
    test runs through ``httpx.MockTransport``).

Must never:
    Send anything but GET (the certificate handshake, which sends nothing
    after it; the model chat POST, the Claude POST, the ntfy alert, the
    telemetry POST and the MCP test's handshake and tool list aside); follow
    a redirect (the one signed download aside, with no credentials); talk to
    a host not on the
    allowlist; put a key in a url, an error or a log line. No other module
    may import an HTTP library (import-linter).
"""

from agentzaaheen.core.net.cert import CertProbe
from agentzaaheen.core.net.claude import CLAUDE_HOST, ClaudeClient, ClaudeReply
from agentzaaheen.core.net.client import (
    DownloadPolicy,
    JsonClient,
    NetPolicy,
    check_base_url,
    check_path,
    check_read_query,
)
from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.net.heartbeat import HEALTHCHECKS_PING_HOST, HeartbeatClient, check_ping_url
from agentzaaheen.core.net.mcp_probe import McpListing, McpTool, check_mcp_url, list_tools
from agentzaaheen.core.net.model import (
    SERVER_MODEL_HOST,
    AccessSource,
    AccessToken,
    KeySource,
    ModelClient,
    check_model_url,
)
from agentzaaheen.core.net.otlp import OtlpSender, check_otlp_endpoint
from agentzaaheen.core.net.probe import StatusProbe, check_probe_url
from agentzaaheen.core.net.push import NTFY_HOST, PushClient

__all__ = [
    "CLAUDE_HOST",
    "HEALTHCHECKS_PING_HOST",
    "NTFY_HOST",
    "SERVER_MODEL_HOST",
    "AccessSource",
    "AccessToken",
    "CertProbe",
    "ClaudeClient",
    "ClaudeReply",
    "DownloadPolicy",
    "HeartbeatClient",
    "JsonClient",
    "KeySource",
    "McpListing",
    "McpTool",
    "ModelClient",
    "NetError",
    "NetErrorCode",
    "NetPolicy",
    "OtlpSender",
    "PushClient",
    "StatusProbe",
    "check_base_url",
    "check_mcp_url",
    "check_model_url",
    "check_otlp_endpoint",
    "check_path",
    "check_ping_url",
    "check_probe_url",
    "check_read_query",
    "list_tools",
]
