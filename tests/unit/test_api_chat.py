"""The chat routes: same-origin JSON only, one question at a time, answers from the cards (F05).

A workspace's chat lives at its own address and sees only that workspace (ADR-039);
``/api/chat`` is the Board's, over every workspace's summary line.

A fake backend stands in for the model; no server and no network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.llm_gateway import ChatReply
from agentzaaheen.core.sdk import SourceError, SourceErrorCode

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}


class FakeChat:
    model_id = "local-gemma"

    def __init__(
        self, reply: str = "MRR is USD 1240.00 (sample data).", error: SourceError | None = None
    ) -> None:
        self.reply = reply
        self.error = error
        self.asked: list[tuple[str, str]] = []
        self.workspaces: list[str | None] = []

    async def ask(self, question: str, facts: str, workspace: str | None = None) -> ChatReply:
        self.asked.append((question, facts))
        self.workspaces.append(workspace)
        if self.error is not None:
            raise self.error
        return ChatReply(self.reply)


def client(chat: FakeChat | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), chat=chat), base_url="http://127.0.0.1")


ZAAHEEN = "/api/workspaces/zaaheen/chat"
BOARD = "/api/chat"


def post(
    c: TestClient, body: object, headers: dict[str, str] | None = None, path: str = ZAAHEEN
) -> httpx.Response:
    response: httpx.Response = c.post(path, json=body, headers=OURS if headers is None else headers)
    return response


QUESTION = {"model_id": "local-gemma", "question": "How is revenue?"}


def test_answers_from_the_project_cards() -> None:
    chat = FakeChat()
    response = post(client(chat), QUESTION)
    assert response.status_code == 200
    assert response.json() == {
        "model_id": "local-gemma",
        "answer": "MRR is USD 1240.00 (sample data).",
        "from_sample": True,
        "used": [],
    }
    question, facts = chat.asked[0]
    assert question == "How is revenue?"
    assert "Project: Zaaheen" in facts
    assert "MRR: USD 1240.00" in facts
    assert "SAMPLE" in facts
    assert "Sandoq Kin" not in facts  # another workspace's facts never reach this chat
    assert chat.workspaces == ["zaaheen"]
    assert response.headers["cache-control"] == "no-store"


def test_the_boards_chat_answers_from_every_workspaces_summary_line_only() -> None:
    chat = FakeChat()
    response = post(client(chat), QUESTION, path=BOARD)
    assert response.status_code == 200
    facts = chat.asked[0][1]
    assert "Project: Zaaheen" in facts
    assert "Project: Sandoq Kin" in facts
    assert "Card:" not in facts
    assert chat.workspaces == [None]  # so no tool that reads one workspace's data


def test_the_connected_model_is_the_only_one_marked_available() -> None:
    models = client(FakeChat()).get("/api/models").json()["models"]
    assert {m["id"]: m["available"] for m in models} == {
        "local-gemma": True,
        "cloud-claude": False,
        "cloud-gpt": False,
    }
    unconnected = client(None).get("/api/models").json()["models"]
    assert not any(m["available"] for m in unconnected)


def test_without_a_model_it_says_not_connected() -> None:
    response = post(client(None), QUESTION)
    assert response.status_code == 503
    assert response.json() == {"detail": "chat.not_connected"}


def test_a_model_that_is_not_connected_is_refused() -> None:
    chat = FakeChat()
    response = post(client(chat), {**QUESTION, "model_id": "cloud-claude"})
    assert response.status_code == 503
    assert chat.asked == []


def test_an_unknown_workspace_is_a_404_and_asks_nothing() -> None:
    chat = FakeChat()
    response = post(client(chat), QUESTION, path="/api/workspaces/nope/chat")
    assert response.status_code == 404
    assert response.json() == {"detail": "workspace.not_found"}
    assert chat.asked == []


def test_a_question_cannot_name_its_own_workspace() -> None:
    """Only the address chooses the workspace: a body that tries to is refused, not obeyed."""
    chat = FakeChat()
    for path in (ZAAHEEN, BOARD):
        assert post(client(chat), {**QUESTION, "project_id": "sandoq-kin"}, path=path).status_code == 422
    assert chat.asked == []


def test_a_badly_formed_address_is_refused() -> None:
    chat = FakeChat()
    assert post(client(chat), QUESTION, path="/api/workspaces/Not_A_Slug/chat").status_code == 422
    assert chat.asked == []


@pytest.mark.parametrize(
    "body",
    [
        {**QUESTION, "question": ""},
        {**QUESTION, "question": "x" * 2001},
        {**QUESTION, "extra": "field"},
        {"question": "no model"},
    ],
)
def test_bad_questions_are_refused(body: object) -> None:
    chat = FakeChat()
    assert post(client(chat), body).status_code == 422
    assert chat.asked == []


# ---------------------------------------------------------------- only our own page may ask


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no custom header: a plain cross-site form or fetch
        {REQUEST_HEADER: "0"},
        {REQUEST_HEADER: "1", "Origin": "http://evil.example"},
        {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1:9999"},
        {REQUEST_HEADER: "1", "Origin": "null"},
    ],
)
def test_foreign_requests_are_refused(headers: dict[str, str]) -> None:
    chat = FakeChat()
    response = post(client(chat), QUESTION, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "chat.forbidden"}
    assert chat.asked == []


def test_a_same_origin_request_without_an_origin_header_is_allowed() -> None:
    assert post(client(FakeChat()), QUESTION, headers={REQUEST_HEADER: "1"}).status_code == 200


def test_only_json_is_accepted() -> None:
    chat = FakeChat()
    response = client(chat).post(
        "/api/chat",
        content=b'{"model_id":"local-gemma","question":"hi"}',
        headers={**OURS, "Content-Type": "text/plain"},
    )
    assert response.status_code == 415
    assert chat.asked == []


def test_a_foreign_host_name_is_still_refused() -> None:
    c = TestClient(create_app(SamplePortfolio(NOW), chat=FakeChat()), base_url="http://evil.example")
    assert post(c, QUESTION, headers={REQUEST_HEADER: "1"}).status_code == 400


# ---------------------------------------------------------------- failures and load


def test_a_model_failure_returns_its_code_only() -> None:
    error = SourceError(SourceErrorCode.UNAVAILABLE, "down", detail_code="net.timeout")
    response = post(client(FakeChat(error=error)), QUESTION)
    assert response.status_code == 502
    assert response.json() == {"detail": "net.timeout"}


def test_one_question_at_a_time() -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    class SlowChat(FakeChat):
        async def ask(self, question: str, facts: str, workspace: str | None = None) -> ChatReply:
            started.set()
            await release.wait()
            return ChatReply("done")

    app = create_app(SamplePortfolio(NOW), chat=SlowChat())

    async def scenario() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            first = asyncio.create_task(c.post("/api/chat", json=QUESTION, headers=OURS))
            await started.wait()
            second = await c.post("/api/chat", json=QUESTION, headers=OURS)
            release.set()
            return (await first).status_code, second.status_code

    first_status, second_status = asyncio.run(scenario())
    assert (first_status, second_status) == (200, 429)


def test_the_other_routes_stay_read_only() -> None:
    c = client(FakeChat())
    for method in ("PUT", "PATCH", "DELETE"):
        assert c.request(method, "/api/chat").status_code == 405
    assert c.get("/api/chat").status_code == 405
