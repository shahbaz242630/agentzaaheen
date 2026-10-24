from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.api.app import CONTENT_SECURITY_POLICY

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def ui(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html><title>Yaqzan</title>", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(ui: Path) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), static_dir=ui), base_url="http://127.0.0.1")


def test_lists_projects(client: TestClient) -> None:
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == ["zaaheen", "sandoq-kin", "agent-x", "eva", "family-checkin"]


def test_gets_one_project_with_its_cards(client: TestClient) -> None:
    body = client.get("/api/workspaces/zaaheen").json()
    assert body["project"]["name"] == "Zaaheen"
    assert {c["id"] for c in body["cards"]} >= {"paddle", "stripe", "clients", "support", "github"}


def test_an_unknown_project_is_a_404_with_a_code(client: TestClient) -> None:
    response = client.get("/api/workspaces/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "workspace.not_found"}


def test_lists_models_with_a_local_default(client: TestClient) -> None:
    models = client.get("/api/models").json()["models"]
    defaults = [m for m in models if m["default"]]
    assert len(defaults) == 1
    assert defaults[0]["where"] == "local"


@pytest.mark.parametrize("path", ["/api/workspaces", "/"])
def test_every_response_carries_the_security_headers(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY
    assert "img-src 'self'" in CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_api_answers_are_never_cached(client: TestClient) -> None:
    assert client.get("/api/workspaces").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/").headers


def test_serves_the_web_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>Yaqzan</title>" in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_api_is_read_only(client: TestClient, method: str) -> None:
    assert client.request(method, "/api/models").status_code == 405


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_the_workspace_list_only_reads_and_adds(client: TestClient, method: str) -> None:
    # POST adds a project (F17 slice 5a, test_api_adding.py); nothing changes or deletes the list itself.
    assert client.request(method, "/api/workspaces").status_code == 405


def test_a_foreign_host_name_is_refused(ui: Path) -> None:
    rebinding = TestClient(create_app(SamplePortfolio(NOW), static_dir=ui), base_url="http://evil.example")
    assert rebinding.get("/api/workspaces").status_code == 400


def test_no_api_docs_are_published(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_refuses_to_start_without_a_built_ui(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no built web UI"):
        create_app(SamplePortfolio(NOW), static_dir=tmp_path)


def test_serves_the_api_alone_without_a_ui() -> None:
    client = TestClient(create_app(SamplePortfolio(NOW)), base_url="http://localhost")
    assert client.get("/api/workspaces").status_code == 200
    assert client.get("/").status_code == 404


def test_what_must_be_closed_is_closed_when_the_server_stops() -> None:
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    with TestClient(create_app(SamplePortfolio(NOW), on_close=close), base_url="http://localhost") as running:
        assert running.get("/api/models").status_code == 200
        assert closed == []
    assert closed == [True]
