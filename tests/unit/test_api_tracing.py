"""Each API request is one server span named by its route template; the answer carries its traceparent.

ADR-032, R2: a request seen on screen leads straight to its trace.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.trace import SpanKind, StatusCode

from tests.tracing import Recorded, recorded
from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.api.app import REQUEST_HEADER

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}


@pytest.fixture
def spans() -> Iterator[Recorded]:
    with recorded() as kept:
        yield kept


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "index.html").write_text("<!doctype html><title>Agent Zaaheen</title>", encoding="utf-8")
    return TestClient(create_app(SamplePortfolio(NOW), static_dir=tmp_path), base_url="http://127.0.0.1")


def test_an_api_answer_carries_the_traceparent_of_its_span(client: TestClient, spans: Recorded) -> None:
    response = client.get("/api/workspaces/zaaheen")
    assert response.status_code == 200
    match = TRACEPARENT.fullmatch(response.headers["traceparent"])
    assert match is not None
    done = spans.named("GET /api/workspaces/{slug}")
    assert done.kind is SpanKind.SERVER
    assert done.context is not None
    assert match.group(1) == f"{done.context.trace_id:032x}"
    assert match.group(2) == f"{done.context.span_id:016x}"
    assert dict(done.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/api/workspaces/{slug}",
        "http.response.status_code": 200,
    }
    assert "zaaheen" not in done.to_json()  # the template, never the filled-in path
    assert response.headers["Cache-Control"] == "no-store"


def test_a_refusal_is_traced_but_not_an_error(client: TestClient, spans: Recorded) -> None:
    response = client.get("/api/workspaces/nope")
    assert response.status_code == 404
    assert "traceparent" in response.headers
    done = spans.named("GET /api/workspaces/{slug}")
    assert done.status.status_code is StatusCode.UNSET


def test_a_server_error_marks_the_span(client: TestClient, spans: Recorded) -> None:
    question = {"model_id": "local-gemma", "question": "How is revenue?"}
    response = client.post("/api/chat", json=question, headers=OURS)
    assert response.status_code == 503
    done = spans.named("POST /api/chat")
    assert done.status.status_code is StatusCode.ERROR
    assert (done.attributes or {})["error.type"] == "503"
    assert "How is revenue" not in done.to_json()


def test_an_unknown_api_path_is_unmatched(client: TestClient, spans: Recorded) -> None:
    client.get("/api/nothing/here")
    done = spans.named("GET unmatched")
    assert (done.attributes or {})["http.route"] == "unmatched"


def test_a_traceparent_sent_to_us_is_ignored(client: TestClient, spans: Recorded) -> None:
    theirs = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    response = client.get("/api/workspaces", headers={"traceparent": theirs})
    assert not response.headers["traceparent"].startswith("00-" + "a" * 32)
    done = spans.named("GET /api/workspaces")
    assert done.parent is None


def test_the_web_ui_is_not_traced(client: TestClient, spans: Recorded) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "traceparent" not in response.headers
    assert spans.spans == ()


def test_without_tracing_there_is_no_traceparent(client: TestClient) -> None:
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert "traceparent" not in response.headers
