"""The telemetry sender: one gateway, https only, no redirects or retries, dropped on failure (ADR-032).

No real network: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import base64
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from yaqzan.core.net import NetError, NetErrorCode, OtlpSender, check_otlp_endpoint
from yaqzan.core.net.otlp import MAX_BODY_BYTES

ENDPOINT = "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp"
FAKE_TOKEN = "glc_" + "t" * 40  # built here, never a real token

Handler = Callable[[httpx.Request], httpx.Response]


def sender(handler: Handler) -> OtlpSender:
    return OtlpSender(
        ENDPOINT, user="123456", token=SecretStr(FAKE_TOKEN), transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://otlp-gateway-prod-eu-west-2.grafana.net/otlp",
        "https://otlp-gateway-prod-eu-west-2.grafana.net.evil.com/otlp",
        "https://evil.com/otlp",
        "https://1.2.3.4/otlp",
        "https://user:pw@otlp-gateway-prod-eu-west-2.grafana.net/otlp",
        "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp?x=1",
        "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp#x",
        "https://otlp-gateway-prod-eu-west-2.grafana.net:8443/otlp",
        "https://otlp-gateway-prod-eu-west-2.grafana.net/../otlp",
    ],
)
def test_refuses_any_other_endpoint(endpoint: str) -> None:
    with pytest.raises(NetError) as caught:
        check_otlp_endpoint(endpoint)
    assert caught.value.net_code is NetErrorCode.BLOCKED


@pytest.mark.parametrize(
    ("endpoint", "clean"),
    [
        (ENDPOINT, ENDPOINT),
        (ENDPOINT + "/", ENDPOINT),
        ("https://OTLP-gateway-prod-eu-west-2.grafana.net:443/otlp", ENDPOINT),
        (
            "https://otlp-gateway-prod-eu-west-2.grafana.net",
            "https://otlp-gateway-prod-eu-west-2.grafana.net",
        ),
    ],
)
def test_accepts_the_gateway(endpoint: str, clean: str) -> None:
    assert check_otlp_endpoint(endpoint) == clean


@pytest.mark.parametrize("user", ["", "abc", "12 3", "1" * 13])
def test_the_user_must_be_a_numeric_id(user: str) -> None:
    with pytest.raises(NetError):
        OtlpSender(ENDPOINT, user=user, token=SecretStr(FAKE_TOKEN))


def test_posts_json_to_the_traces_path_with_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    out = sender(handler)
    assert out.send_traces(b'{"resourceSpans":[]}') is True
    out.close()
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT + "/v1/traces"
    assert request.headers["Content-Type"] == "application/json"
    expected = base64.b64encode(f"123456:{FAKE_TOKEN}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.content == b'{"resourceSpans":[]}'


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "net.auth"),
        (403, "net.auth"),
        (429, "net.rate_limited"),
        (400, "net.client_error"),
        (307, "net.client_error"),
        (503, "net.server_error"),
    ],
)
def test_a_refusal_is_dropped_and_logged_by_code(
    status: int, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.com/"} if status == 307 else {})

    assert sender(handler).send_traces(b"{}") is False
    assert len(calls) == 1  # never retried, never redirected
    logged = capsys.readouterr().err
    assert reason in logged
    assert f'"http_status": {status}' in logged
    assert FAKE_TOKEN not in logged


@pytest.mark.parametrize(
    ("error", "reason"),
    [(httpx.ReadTimeout("slow"), "net.timeout"), (httpx.ConnectError("down"), "net.unavailable")],
)
def test_a_failed_send_is_dropped(error: Exception, reason: str, capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    assert sender(handler).send_traces(b"{}") is False
    assert reason in capsys.readouterr().err


def test_an_oversized_batch_is_dropped_before_sending(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    assert sender(handler).send_traces(b"x" * (MAX_BODY_BYTES + 1)) is False
    assert calls == []
    assert "net.too_large" in capsys.readouterr().err


def test_a_batch_at_the_cap_is_sent() -> None:
    assert sender(lambda _r: httpx.Response(204)).send_traces(b"x" * MAX_BODY_BYTES) is True


@pytest.mark.parametrize(("method", "path"), [("send_metrics", "/v1/metrics"), ("send_logs", "/v1/logs")])
def test_metrics_and_logs_go_to_their_own_paths(method: str, path: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    assert getattr(sender(handler), method)(b"{}") is True
    assert [str(r.url) for r in seen] == [ENDPOINT + path]


def test_a_drop_says_which_signal(capsys: pytest.CaptureFixture[str]) -> None:
    assert sender(lambda _r: httpx.Response(401)).send_logs(b"{}") is False
    assert '"kind": "logs"' in capsys.readouterr().err
