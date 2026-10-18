"""The sandbox check tool: numbers only, never the key or ids, refuses live keys."""

from __future__ import annotations

import httpx
import pytest

from tests.fixtures import paddle_world as world
from tools import paddle_check

FAKE_KEY = "pdl_sdbx_apikey_" + "q" * 20  # built here, never a real key


def run_tool(environ: dict[str, str], transport: httpx.AsyncBaseTransport | None = None) -> int:
    return paddle_check.main(["--key-env", "K"], environ=environ, transport=transport, now=world.NOW)


def test_prints_the_metrics_and_nothing_identifying(capsys: pytest.CaptureFixture[str]) -> None:
    paddle = world.PaddleWorld()
    assert run_tool({"K": FAKE_KEY}, httpx.MockTransport(paddle.serve)) == 0
    out = capsys.readouterr().out
    assert "MRR (minor units per currency): {'GBP': 300, 'USD': 1300}" in out
    assert "active subscribers: 3   trials: 1" in out
    assert FAKE_KEY not in out
    assert "sub_" not in out
    assert "ctm_" not in out
    assert all(r.url.host == "sandbox-api.paddle.com" for r in paddle.requests)


def test_missing_key_is_could_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool({}) == 1
    assert "could not run" in capsys.readouterr().out


def test_a_live_key_is_refused_before_any_request(capsys: pytest.CaptureFixture[str]) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent with a live key")

    assert run_tool({"K": "pdl_live_apikey_" + "q" * 20}, httpx.MockTransport(serve)) == 1
    assert "LIVE" in capsys.readouterr().out


def test_a_refusal_prints_only_the_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool({"K": FAKE_KEY}, httpx.MockTransport(lambda r: httpx.Response(403))) == 1
    out = capsys.readouterr().out
    assert "source.auth" in out
    assert FAKE_KEY not in out


def test_a_trailing_newline_is_stripped_but_inner_spaces_are_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    paddle = world.PaddleWorld()
    assert run_tool({"K": FAKE_KEY + "\r\n"}, httpx.MockTransport(paddle.serve)) == 0
    assert all(r.headers["Authorization"] == f"Bearer {FAKE_KEY}" for r in paddle.requests)
    assert run_tool({"K": "pdl sdbx"}) == 1
    assert "copy it again" in capsys.readouterr().out
