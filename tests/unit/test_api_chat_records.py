"""Every chat kept as a record (F19 slice 1): the chat routes hand each answered turn to the records.

The workspace kept in comes from the address (``None`` for the Board's chat).
A question that was not answered keeps nothing. If keeping fails, the answer
is still shown and a log line says it was not kept, without the words.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from agentzaaheen.core.store import StoreError, StoreErrorCode, WorkspaceRow
from tests.unit.test_api_chat import BOARD, NOW, QUESTION, ZAAHEEN, FakeChat, post
from tests.unit.test_log_door_lines import lines


class FakeRecords:
    def __init__(self, error: StoreError | None = None) -> None:
        self.error = error
        self.kept: list[tuple[str | None, str, str, str, bool]] = []

    async def keep(
        self, workspace: WorkspaceRow | None, question: str, answer: str, *, model_id: str, from_sample: bool
    ) -> None:
        if self.error is not None:
            raise self.error
        self.kept.append(
            (None if workspace is None else workspace.slug, question, answer, model_id, from_sample)
        )


def client(chat: FakeChat, records: FakeRecords | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), chat=chat, chats=records), base_url="http://127.0.0.1")


def test_a_workspace_turn_is_kept_in_that_workspace() -> None:
    records = FakeRecords()
    response = post(client(FakeChat(reply="All well."), records), QUESTION, path=ZAAHEEN)
    assert response.status_code == 200
    assert records.kept == [("zaaheen", "How is revenue?", "All well.", "local-gemma", True)]


def test_the_boards_turn_is_kept_as_the_boards() -> None:
    records = FakeRecords()
    assert post(client(FakeChat(reply="All well."), records), QUESTION, path=BOARD).status_code == 200
    assert [k[0] for k in records.kept] == [None]


def test_an_unanswered_question_keeps_nothing() -> None:
    records = FakeRecords()
    failing = FakeChat(error=SourceError(SourceErrorCode.UNAVAILABLE, "down"))
    assert post(client(failing, records), QUESTION).status_code == 502
    assert records.kept == []


def test_a_failed_keep_still_shows_the_answer_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    records = FakeRecords(error=StoreError(StoreErrorCode.UNAVAILABLE, "down"))
    response = post(client(FakeChat(reply="Secret words here."), records), QUESTION)
    assert response.status_code == 200
    assert response.json()["answer"] == "Secret words here."
    (line,) = lines(capsys, "chat_record_failed")
    assert line["workspace"] == "zaaheen"
    assert line["code"] == "store.unavailable"
    assert "Secret words" not in str(line)
    assert "How is revenue" not in str(line)


def test_without_records_nothing_is_kept_and_it_still_answers() -> None:
    assert post(client(FakeChat(), None), QUESTION).status_code == 200
