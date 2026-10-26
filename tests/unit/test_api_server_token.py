"""The server token routes: save after Windows Hello, never send either part back (F10 slice 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import ServerTokenStatus, ServerTokenVerdict, token_problem
from agentzaaheen.core.store import StoreError, StoreErrorCode

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
ID = "0" * 32 + ".access"  # built here, never a real token
SECRET = "f" * 64
SIGNED = {"id": "cred", "response": {"signature": "sig"}}


class FakeTokens:
    """Saves like the real one: only with a signature, and only parts that look like a token."""

    def __init__(self) -> None:
        self.saved: tuple[str, str] | None = None
        self.broken = False

    async def status(self) -> ServerTokenStatus:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return ServerTokenStatus(
            vault=True, saved=self.saved is not None, saved_at=NOW if self.saved else None
        )

    async def options(self, client_id: str, client_secret: SecretStr) -> dict[str, Any]:
        if token_problem(client_id, client_secret.get_secret_value()) is not None:
            raise PasskeyError(ServerTokenVerdict.BAD_TOKEN.value)
        return {"challenge": "c"}

    async def save(
        self, client_id: str, client_secret: SecretStr, credential: dict[str, Any] | None
    ) -> ServerTokenVerdict:
        if token_problem(client_id, client_secret.get_secret_value()) is not None:
            return ServerTokenVerdict.BAD_TOKEN
        if credential is None:
            return ServerTokenVerdict.NEEDS_PASSKEY
        self.saved = (client_id, client_secret.get_secret_value())
        return ServerTokenVerdict.SAVED


def client(tokens: FakeTokens | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), server_token=tokens), base_url="http://127.0.0.1")


def post(c: TestClient, path: str, body: object) -> httpx.Response:
    response: httpx.Response = c.post(path, json=body, headers=OURS)
    return response


def test_saving_asks_windows_hello_then_says_only_that_a_token_is_saved() -> None:
    fake = FakeTokens()
    c = client(fake)
    assert c.get("/api/server-token").json() == {
        "switches": True,
        "vault": True,
        "saved": False,
        "saved_at": None,
    }
    both = {"client_id": ID, "client_secret": SECRET}
    unsigned = post(c, "/api/server-token", both)
    assert (unsigned.status_code, unsigned.json()) == (409, {"detail": "server_token.needs_passkey"})
    assert post(c, "/api/server-token/options", both).json() == {"options": {"challenge": "c"}}
    done = post(c, "/api/server-token", {**both, "credential": SIGNED})
    assert done.status_code == 200
    assert done.json()["saved"] is True
    assert fake.saved == (ID, SECRET)
    assert SECRET not in done.text
    assert ID not in done.text


def test_neither_part_is_ever_sent_back_even_when_refused() -> None:
    c = client(FakeTokens())
    for path, body in (
        ("/api/server-token", {"client_id": "x" + SECRET, "client_secret": SECRET}),
        ("/api/server-token/options", {"client_id": ID, "client_secret": SECRET + " "}),
        ("/api/server-token", {"client_id": ID, "client_secret": SECRET, "extra": "x"}),
        ("/api/server-token", {"client_id": ID, "client_secret": SECRET, "credential": "not an object"}),
        ("/api/server-token", {"client_id": ID}),
        ("/api/server-token", {"client_secret": SECRET}),  # a missing part must not echo the other
    ):
        response = post(c, path, body)
        assert response.status_code in {409, 422}
        assert SECRET not in response.text
        assert ID not in response.text


def test_without_a_database_the_token_cannot_be_saved_here() -> None:
    c = client(None)
    assert c.get("/api/server-token").json() == {
        "switches": False,
        "vault": False,
        "saved": False,
        "saved_at": None,
    }
    refused = post(c, "/api/server-token", {"client_id": ID, "client_secret": SECRET, "credential": SIGNED})
    assert (refused.status_code, refused.json()) == (503, {"detail": "server_token.read_only"})


def test_a_page_from_elsewhere_is_refused() -> None:
    fake = FakeTokens()
    c = client(fake)
    response = c.post(
        "/api/server-token", json={"client_id": ID, "client_secret": SECRET, "credential": SIGNED}
    )
    assert response.status_code == 403
    assert fake.saved is None


def test_a_database_that_does_not_answer_is_said_plainly() -> None:
    fake = FakeTokens()
    fake.broken = True
    response = client(fake).get("/api/server-token")
    assert (response.status_code, response.json()) == (503, {"detail": "server_token.store_unavailable"})
