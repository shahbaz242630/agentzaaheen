"""The live Claude check tool: one capped call through the gate and the door, never the key (F10 slice 2b).

No real network and no real key: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tools import claude_check

FAKE_KEY = "sk-ant-api03-" + "q" * 24  # built here, never a real key


def answered(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "The connection to Claude works."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 40, "output_tokens": 9},
        },
    )


def run_tool(environ: dict[str, str], handler: object = answered, *extra: str, key_env: str = "K") -> int:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]  # test handlers are plain functions
    return claude_check.main(["--key-env", key_env, *extra], environ=environ, transport=transport)


def test_one_capped_call_answers_and_says_what_it_cost(capsys: pytest.CaptureFixture[str]) -> None:
    sent: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answered(request)

    assert run_tool({"K": FAKE_KEY}, serve) == 0
    out = capsys.readouterr().out
    assert "claude-sonnet-5-5 answered." in out
    assert "Answer: The connection to Claude works." in out
    assert "Cost: USD 0.0002 (not added to the monthly caps)" in out  # 40 x 2 + 9 x 10 millionths, rounded up
    assert FAKE_KEY not in out
    (request,) = sent
    assert request.url.host == "api.anthropic.com"
    body = json.loads(request.content)
    assert (body["model"], body["max_tokens"]) == ("claude-sonnet-5-5", 64)  # capped: well under one cent


def test_opus_can_be_checked_too(capsys: pytest.CaptureFixture[str]) -> None:
    sent: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["model"])
        return answered(request)

    assert run_tool({"K": FAKE_KEY}, serve, "--model", "opus") == 0
    assert sent == ["claude-opus-5-5"]
    assert "claude-opus-5-5 answered." in capsys.readouterr().out


def refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request may be sent")


def test_the_claude_code_key_name_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool({"ANTHROPIC_API_KEY": FAKE_KEY}, refuse, key_env="ANTHROPIC_API_KEY") == 1
    assert "YAQZAN_CLAUDE_KEY" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("environ", "said"),
    [
        ({}, "could not run"),
        ({"K": "sk-ant- api03"}, "spaces or control characters"),
        ({"K": "pdl_sdbx_apikey_" + "q" * 20}, "does not look like a Claude API key"),
    ],
)
def test_a_missing_or_odd_key_is_refused_before_any_request(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str], said: str
) -> None:
    assert run_tool(environ, refuse) == 1
    assert said in capsys.readouterr().out


def test_a_bad_key_says_what_why_and_what_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool({"K": FAKE_KEY}, lambda r: httpx.Response(401)) == 1
    out = capsys.readouterr().out
    assert "failed with net.auth, http 401." in out
    assert "What: Claude refused our API key." in out
    assert "What to do:" in out
    assert FAKE_KEY not in out


def test_used_up_credit_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool({"K": FAKE_KEY}, lambda r: httpx.Response(402)) == 1
    out = capsys.readouterr().out
    assert "failed with net.client_error, http 402." in out
    assert "the API credit for this month is used up" in out


def test_a_declined_question_is_not_a_pass(capsys: pytest.CaptureFixture[str]) -> None:
    def decline(request: httpx.Request) -> httpx.Response:
        reply = answered(request).json()
        return httpx.Response(200, json={**reply, "content": [], "stop_reason": "refusal"})

    assert run_tool({"K": FAKE_KEY}, decline) == 1
    assert "did not answer: model.declined." in capsys.readouterr().out
