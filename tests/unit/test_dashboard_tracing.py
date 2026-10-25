"""The dashboard's tracing options (ADR-032): off unless asked, all three at once, the token never printed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentzaaheen.core.sdk import span
from tools import dashboard
from tools.dashboard_tracing import TracingNotStartedError, add_tracing_options, tracing_from

ENDPOINT = "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp"
FAKE_TOKEN = "glc_" + "t" * 40  # built here, never a real token
ENV = {"OTEL_TOKEN": FAKE_TOKEN}
FULL = ["--otel-token-env", "OTEL_TOKEN", "--otel-endpoint", ENDPOINT, "--otel-user", "123456"]


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_tracing_options(parser)
    return parser.parse_args(argv)


def test_off_unless_asked() -> None:
    assert tracing_from(parse([]), ENV) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--otel-token-env", "OTEL_TOKEN"],
        ["--otel-endpoint", ENDPOINT, "--otel-user", "1"],
        ["--otel-token-env", "OTEL_TOKEN", "--otel-user", "1"],
    ],
)
def test_all_three_options_or_none(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(TracingNotStartedError):
        tracing_from(parse(argv), ENV)
    assert "together" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["", "   ", "has space", "tab\there"])
def test_an_unusable_token_is_refused(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(TracingNotStartedError):
        tracing_from(parse(FULL), {"OTEL_TOKEN": value})
    assert "OTEL_TOKEN holds no usable token" in capsys.readouterr().out


def test_a_missing_variable_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(TracingNotStartedError):
        tracing_from(parse(FULL), {})
    assert "no usable token" in capsys.readouterr().out


def test_a_wrong_endpoint_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--otel-token-env", "OTEL_TOKEN", "--otel-endpoint", "https://evil.com/otlp", "--otel-user", "1"]
    with pytest.raises(TracingNotStartedError):
        tracing_from(parse(argv), ENV)
    out = capsys.readouterr().out
    assert "net.blocked" in out
    assert FAKE_TOKEN not in out


def test_started_tracing_sends_spans_and_never_prints_the_token(capsys: pytest.CaptureFixture[str]) -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/traces"):
            bodies.append(request.content)
        return httpx.Response(200)

    tracing = tracing_from(parse(FULL), ENV, transport=httpx.MockTransport(handler))
    assert tracing is not None
    with span("hello"):
        pass
    tracing.shutdown()
    assert json.loads(bodies[0])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "hello"
    out = capsys.readouterr()
    assert FAKE_TOKEN not in out.out + out.err


def test_the_dashboard_refuses_to_start_with_half_the_options(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    served: list[FastAPI] = []
    code = dashboard.main(
        ["--otel-token-env", "OTEL_TOKEN"],
        static_dir=tmp_path,
        serve=lambda app, _h, _p: served.append(app),
        environ=ENV,
    )
    assert code == 1
    assert served == []


def test_the_dashboard_traces_while_it_serves_and_stops_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    bodies: list[bytes] = []

    def keep(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/traces"):
            bodies.append(request.content)
        return httpx.Response(200)

    transport = httpx.MockTransport(keep)
    monkeypatch.setattr(
        "tools.dashboard.tracing_from", lambda ns, env: tracing_from(ns, env, transport=transport)
    )
    headers: list[str | None] = []

    def serve(app: FastAPI, _host: str, _port: int) -> None:
        headers.append(
            TestClient(app, base_url="http://127.0.0.1").get("/api/workspaces").headers.get("traceparent")
        )

    assert dashboard.main(FULL, static_dir=tmp_path, serve=serve, environ=ENV) == 0
    assert headers[0] is not None
    names = {
        s["name"]
        for b in bodies
        for r in json.loads(b)["resourceSpans"]
        for sc in r["scopeSpans"]
        for s in sc["spans"]
    }
    assert {"dashboard start", "GET /api/workspaces"} <= names
    with span("after") as current:
        assert not current.is_recording()
