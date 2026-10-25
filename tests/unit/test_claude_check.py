"""The live Claude check tool: one capped call through the gate and the door, never the key (F10 2b, 2c).

No real network, no real key and no database: every request goes to an ``httpx.MockTransport``
handler, the key comes from a fixed source and the spend book is kept in memory. The run with
the key sealed in the database is in ``tests/integration/test_claude_check_tool.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import MODELS, MemoryBook, MemoryMisses, Use
from agentzaaheen.core.sdk import ModelLevel, SourceError, SourceErrorCode
from tools import claude_check
from tools.name_prints import make_key

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


class Book(MemoryBook):
    """The spend book in memory, remembering each call it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Use, str, int]] = []

    async def add(self, use: Use, model: str, micro: int, at: object) -> None:
        self.calls.append((use, model, micro))
        await super().add(use, model, micro, at)  # type: ignore[arg-type]  # the time is passed on as given


def run_tool(handler: object = answered, *extra: str, book: Book | None = None) -> int:
    """The check and its report, as ``main`` runs them once the key is opened from the vault."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]  # test handlers are plain functions

    async def key() -> SecretStr:
        return SecretStr(FAKE_KEY)

    level = claude_check.LEVELS[extra[1]] if extra else ModelLevel.SONNET
    answer = asyncio.run(claude_check.check(key, level, book or Book(), MemoryMisses(), transport))
    code, lines = claude_check.report(answer, MODELS[level].model_id)
    sys.stdout.write("\n".join(lines) + "\n")  # as main prints them
    return code


def test_one_capped_call_answers_and_says_what_it_cost(capsys: pytest.CaptureFixture[str]) -> None:
    sent: list[httpx.Request] = []
    book = Book()

    def serve(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answered(request)

    assert run_tool(serve, book=book) == 0
    out = capsys.readouterr().out
    assert "claude-sonnet-5-5 answered." in out
    assert "Answer: The connection to Claude works." in out
    assert "Cost: USD 0.0002 (added to this month's spend)" in out  # 40 x 2 + 9 x 10 millionths, rounded up
    assert book.calls == [(Use.CODE_REVIEW, "claude-sonnet-5-5", 170)]
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

    assert run_tool(serve, "--model", "opus") == 0
    assert sent == ["claude-opus-5-5"]
    assert "claude-opus-5-5 answered." in capsys.readouterr().out


def refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request may be sent")


def test_no_saved_key_says_to_save_it_on_the_customize_page(capsys: pytest.CaptureFixture[str]) -> None:
    async def missing() -> SecretStr:
        raise SourceError(SourceErrorCode.AUTH, "no key", detail_code="credential.missing")

    answer = asyncio.run(
        claude_check.check(missing, ModelLevel.SONNET, Book(), MemoryMisses(), httpx.MockTransport(refuse))
    )
    code, lines = claude_check.report(answer, "claude-sonnet-5-5")
    assert code == 1
    assert "no Claude key is saved" in lines[0]
    assert "Customize page" in lines[1]


def test_without_the_vault_key_file_nothing_is_tried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def no_database() -> SecretStr | None:
        raise AssertionError("the database may not be opened")

    assert claude_check.main([], key_file=tmp_path / "missing.key", prepare=no_database) == 1
    out = capsys.readouterr().out
    assert "no master key at" in out
    assert "could not run, not a pass" in out


def test_without_the_laptop_database_nothing_is_tried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "vault.key"
    assert make_key(key_file, "vault-1")

    async def stopped() -> SecretStr | None:
        return None

    assert (
        claude_check.main([], key_file=key_file, prepare=stopped, transport=httpx.MockTransport(refuse)) == 1
    )
    assert "the laptop database is not running" in capsys.readouterr().out


def test_a_bad_key_says_what_why_and_what_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool(lambda r: httpx.Response(401)) == 1
    out = capsys.readouterr().out
    assert "failed with net.auth, http 401." in out
    assert "What: Claude refused our API key." in out
    assert "What to do:" in out
    assert FAKE_KEY not in out


def test_used_up_credit_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool(lambda r: httpx.Response(402)) == 1
    out = capsys.readouterr().out
    assert "failed with net.client_error, http 402." in out
    assert "the API credit for this month is used up" in out


def test_a_declined_question_is_not_a_pass(capsys: pytest.CaptureFixture[str]) -> None:
    def decline(request: httpx.Request) -> httpx.Response:
        reply = answered(request).json()
        return httpx.Response(200, json={**reply, "content": [], "stop_reason": "refusal"})

    assert run_tool(decline) == 1
    assert "did not answer: model.declined." in capsys.readouterr().out
