"""Send the dashboard's traces to the trace store while it runs (ADR-032, F07 step 3).

Off unless asked for. ``--otel-token-env NAME`` names the environment variable
holding the SEND-ONLY Grafana token; ``--otel-endpoint`` is the stack's OTLP
gateway and ``--otel-user`` its numeric stack id (neither is secret, but they
name the Chairman's account, so they stay out of the repo and are passed in).
The token is never printed. If the store is down, spans are dropped: the
dashboard works the same either way.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from importlib.metadata import version

import httpx
from pydantic import SecretStr

from tools.dashboard_sources import usable_secret
from yaqzan.core.net import NetError, OtlpSender
from yaqzan.core.telemetry import Tracing, start_tracing


def add_tracing_options(parser: argparse.ArgumentParser) -> None:
    """The three tracing options; all or none."""
    parser.add_argument("--otel-token-env", help="env var holding the SEND-ONLY Grafana telemetry token")
    parser.add_argument(
        "--otel-endpoint", help="the stack's OTLP gateway (https://otlp-gateway-...grafana.net/otlp)"
    )
    parser.add_argument("--otel-user", help="the Grafana stack id (numbers only)")


class TracingNotStartedError(Exception):
    """Tracing was asked for but cannot start; the reason has been printed."""


def tracing_from(
    ns: argparse.Namespace, env: Mapping[str, str], *, transport: httpx.BaseTransport | None = None
) -> Tracing | None:
    """Tracing as asked for, or ``None`` if not asked. Raises ``TracingNotStartedError`` after saying why."""
    given = (ns.otel_token_env, ns.otel_endpoint, ns.otel_user)
    if not any(given):
        return None
    if not all(given):
        print("dashboard: tracing needs --otel-token-env, --otel-endpoint and --otel-user together.")
        raise TracingNotStartedError
    raw = env.get(ns.otel_token_env, "").strip()
    if not raw or not usable_secret(raw):
        print(f"dashboard: the environment variable {ns.otel_token_env} holds no usable token.")
        raise TracingNotStartedError
    try:
        sender = OtlpSender(ns.otel_endpoint, user=ns.otel_user, token=SecretStr(raw), transport=transport)
    except NetError as exc:
        print(f"dashboard: tracing not started ({exc.reason_code}); check --otel-endpoint and --otel-user.")
        raise TracingNotStartedError from None
    print("dashboard: traces go to the trace store (send-only token; dropped if the store is down).")
    return start_tracing(sender, version=version("yaqzan"))
