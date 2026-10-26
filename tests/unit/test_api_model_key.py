"""The server model key routes: save after Windows Hello, never send the key back (F10 slice 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import ModelKeyStatus, ModelKeyVerdict, model_key_problem
from agentzaaheen.core.store import StoreError, StoreErrorCode

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
KEY = "a1" * 32  # built here, never the real one
SIGNED = {"id": "cred", "response": {"signature": "sig"}}


class FakeKeys:
    """Saves like the real one: only with a signature, and only a key that looks like one."""

    def __init__(self) -> None:
        self.saved: str | None = None
        self.broken = False

    async def status(self) -> ModelKeyStatus:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return ModelKeyStatus(vault=True, saved=self.saved is not None, saved_at=NOW if self.saved else None)

    async def options(self, key: SecretStr) -> dict[str, Any]:
        if model_key_problem(key.get_secret_value()) is not None:
            raise PasskeyError(ModelKeyVerdict.BAD_KEY.value)
        return {"challenge": "c"}

    async def save(self, key: SecretStr, credential: dict[str, Any] | None) -> ModelKeyVerdict:
        if model_key_problem(key.get_secret_value()) is not None:
            return ModelKeyVerdict.BAD_KEY
        if credential is None:
            return ModelKeyVerdict.NEEDS_PASSKEY
        self.saved = key.get_secret_value()
        return ModelKeyVerdict.SAVED


def client(keys: FakeKeys | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), model_key=keys), base_url="http://127.0.0.1")


def post(c: TestClient, path: str, body: object) -> httpx.Response:
    response: httpx.Response = c.post(path, json=body, headers=OURS)
    return response


def test_saving_asks_windows_hello_then_says_only_that_a_key_is_saved() -> None:
    fake = FakeKeys()
    c = client(fake)
    assert c.get("/api/model-key").json() == {
        "switches": True,
        "vault": True,
        "saved": False,
        "saved_at": None,
    }
    unsigned = post(c, "/api/model-key", {"key": KEY})
    assert (unsigned.status_code, unsigned.json()) == (409, {"detail": "model_key.needs_passkey"})
    assert post(c, "/api/model-key/options", {"key": KEY}).json() == {"options": {"challenge": "c"}}
    done = post(c, "/api/model-key", {"key": KEY, "credential": SIGNED})
    assert done.status_code == 200
    assert done.json()["saved"] is True
    assert fake.saved == KEY
    assert KEY not in done.text


def test_the_key_is_never_sent_back_even_when_refused() -> None:
    c = client(FakeKeys())
    for path, body in (
        ("/api/model-key", {"key": KEY + " "}),
        ("/api/model-key/options", {"key": KEY[:10]}),
        ("/api/model-key", {"key": KEY, "extra": "x"}),
        ("/api/model-key", {"key": KEY, "credential": "not an object"}),
        ("/api/model-key", {"credential": SIGNED}),
    ):
        response = post(c, path, body)
        assert response.status_code in {409, 422}
        assert KEY not in response.text
        assert KEY[:10] not in response.text


def test_without_a_database_the_key_cannot_be_saved_here() -> None:
    c = client(None)
    assert c.get("/api/model-key").json() == {
        "switches": False,
        "vault": False,
        "saved": False,
        "saved_at": None,
    }
    refused = post(c, "/api/model-key", {"key": KEY, "credential": SIGNED})
    assert (refused.status_code, refused.json()) == (503, {"detail": "model_key.read_only"})


def test_a_page_from_elsewhere_is_refused() -> None:
    fake = FakeKeys()
    response = client(fake).post("/api/model-key", json={"key": KEY, "credential": SIGNED})
    assert response.status_code == 403
    assert fake.saved is None


def test_a_database_that_does_not_answer_is_said_plainly() -> None:
    fake = FakeKeys()
    fake.broken = True
    response = client(fake).get("/api/model-key")
    assert (response.status_code, response.json()) == (503, {"detail": "model_key.store_unavailable"})
