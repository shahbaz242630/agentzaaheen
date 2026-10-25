"""Each door really writes its line (ADR-034, slice 2): run the door, read what it logged.

``test_log_doors`` checks the log points are in the code; these check what they say,
and that a line never carries what it must not (paths, filled-in IDs, untrusted names).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tools.dashboard_sources import ListNotUsableError
from tools.dashboard_uptime import uptime_list
from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.llm_gateway import FactChat
from yaqzan.core.llm_gateway.tools import Tool
from yaqzan.core.net import JsonClient, ModelClient, NetError, NetPolicy, PushClient, StatusProbe
from yaqzan.core.store import StoreError, StoreErrorCode
from yaqzan.core.store.db import Database, changed, written

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
HOST = "api.example.com"


def lines(capsys: pytest.CaptureFixture[str], event: str) -> list[dict[str, Any]]:
    """The JSON log lines written to stderr for ``event``."""
    out = []
    for raw in capsys.readouterr().err.splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict) and line.get("event") == event:
            out.append(line)
    return out


# --- the API --------------------------------------------------------------------------------------


def test_every_api_request_is_one_line_with_its_route_template(capsys: pytest.CaptureFixture[str]) -> None:
    client = TestClient(create_app(SamplePortfolio(NOW)), base_url="http://127.0.0.1")
    capsys.readouterr()
    assert client.get("/api/workspaces").status_code == 200
    [line] = lines(capsys, "api_answered")
    assert (line["method"], line["route"], line["http_status"]) == ("GET", "/api/workspaces", 200)
    assert line["what"].startswith("The dashboard asked for GET /api/workspaces and got http 200 in ")
    assert line["level"] == "info"


def test_a_request_inside_a_workspace_names_the_workspace_the_door_chose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = TestClient(create_app(SamplePortfolio(NOW)), base_url="http://127.0.0.1")
    capsys.readouterr()
    assert client.get("/api/workspaces/zaaheen").status_code == 200
    [line] = lines(capsys, "workspace_api_answered")
    assert (line["route"], line["workspace"], line["http_status"]) == (
        "/api/workspaces/{slug}",
        "zaaheen",
        200,
    )
    assert line["what"].startswith(
        "The dashboard asked for GET /api/workspaces/{slug} in the zaaheen workspace and got http 200 in "
    )


def test_a_refusal_is_answered_not_failed(capsys: pytest.CaptureFixture[str]) -> None:
    client = TestClient(create_app(SamplePortfolio(NOW)), base_url="http://127.0.0.1")
    capsys.readouterr()
    assert client.get("/api/workspaces/nope").status_code == 404
    [line] = lines(capsys, "api_answered")
    assert line["http_status"] == 404
    assert "workspace" not in line  # a refused address is never logged as if it were a workspace
    assert "nope" not in line["what"]


def test_a_crash_inside_a_request_is_an_error_line(capsys: pytest.CaptureFixture[str]) -> None:
    app = create_app(SamplePortfolio(NOW))

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("broken on purpose")

    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    capsys.readouterr()
    assert client.get("/api/boom").status_code == 500
    [line] = lines(capsys, "api_failed")
    assert (line["level"], line["http_status"], line["route"]) == ("error", 500, "/api/boom")
    assert "broken on purpose" not in json.dumps(line)  # the error's own text stays out
    assert line["next"].startswith("Open this request's trace")


# --- outside calls ------------------------------------------------------------------------------


def json_get(handler: Any) -> object:  # an httpx handler
    async def body() -> object:
        policy = NetPolicy(allowed_hosts=frozenset({HOST}), max_attempts=2)
        transport = httpx.MockTransport(handler)

        async def no_wait(_: float) -> None:
            return None

        async with JsonClient(f"https://{HOST}", policy=policy, transport=transport, sleep=no_wait) as c:
            return await c.get_json("/things/secret-name", {})

    return asyncio.run(body())


def test_a_connector_call_says_who_answered_and_how_fast(capsys: pytest.CaptureFixture[str]) -> None:
    json_get(lambda request: httpx.Response(200, json={"ok": True}))
    [line] = lines(capsys, "net_answered")
    assert (line["host"], line["attempt"], line["http_status"]) == (HOST, 1, 200)
    assert "secret-name" not in json.dumps(line)  # never the path


def test_a_failed_connector_call_says_why_and_after_how_many_tries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(NetError):
        json_get(lambda request: httpx.Response(503))
    [line] = lines(capsys, "net_failed")
    assert (line["level"], line["host"], line["attempt"], line["http_status"]) == ("warning", HOST, 2, 503)
    assert line["code"] == "net.server_error"
    assert line["why"]
    assert line["next"]


def test_the_uptime_probe_logs_answers_and_silence(capsys: pytest.CaptureFixture[str]) -> None:
    async def probe(handler: Any) -> None:  # an httpx handler
        async with StatusProbe(frozenset({HOST}), transport=httpx.MockTransport(handler)) as p:
            await p.status_of(f"https://{HOST}/health")

    asyncio.run(probe(lambda request: httpx.Response(204)))
    [line] = lines(capsys, "probe_answered")
    assert (line["host"], line["http_status"]) == (HOST, 204)

    def silent(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(NetError):
        asyncio.run(probe(silent))
    [line] = lines(capsys, "probe_failed")
    assert (line["host"], line["code"]) == (HOST, "net.unavailable")


def test_the_model_logs_how_long_it_took(capsys: pytest.CaptureFixture[str]) -> None:
    async def ask() -> str:
        reply = {"choices": [{"message": {"content": "hi"}}]}
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=reply))
        async with ModelClient("http://127.0.0.1:8766", transport=transport) as model:
            return await model.chat([{"role": "user", "content": "q"}], max_tokens=5, temperature=0)

    assert asyncio.run(ask()) == "hi"
    [line] = lines(capsys, "model_answered")
    assert isinstance(line["duration_ms"], int)
    assert "hi" not in line["what"]


def test_a_refused_phone_alert_is_logged_without_the_topic(capsys: pytest.CaptureFixture[str]) -> None:
    topic = SecretStr("abcdefghijklmnop" + "qrstuvwx")

    async def send() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(429))
        async with PushClient(topic, transport=transport) as pusher:
            await pusher.send("t", "m")

    with pytest.raises(NetError):
        asyncio.run(send())
    [line] = lines(capsys, "push_failed")
    assert (line["http_status"], line["code"]) == (429, "net.client_error")
    assert topic.get_secret_value() not in json.dumps(line)


# --- database writes -----------------------------------------------------------------------------


class _Conn:
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class _Pool:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        if self.fail is not None:
            raise self.fail
        yield _Conn()


def write(pool: _Pool, *, atomic: bool = False) -> None:
    async def body() -> None:
        async with Database(pool).write("a test record", atomic=atomic):  # type: ignore[arg-type]  # a fake pool
            pass

    asyncio.run(body())


@pytest.mark.parametrize("atomic", [False, True])
def test_every_write_says_what_kind_of_record_was_written(
    capsys: pytest.CaptureFixture[str], *, atomic: bool
) -> None:
    write(_Pool(), atomic=atomic)
    [line] = lines(capsys, "store_saved")
    assert line["record"] == "a test record"
    assert line["what"].startswith("Wrote a test record to the database in ")


def test_a_failed_write_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(StoreError):
        write(_Pool(fail=ConnectionRefusedError()))
    [line] = lines(capsys, "store_write_failed")
    assert (line["level"], line["record"], line["code"]) == ("warning", "a test record", "store.unavailable")
    assert lines(capsys, "store_saved") == []


def test_a_store_error_raised_inside_the_write_is_logged_too(capsys: pytest.CaptureFixture[str]) -> None:
    async def body() -> None:
        async with Database(_Pool()).write("a test record"):  # type: ignore[arg-type]  # a fake pool
            raise StoreError(StoreErrorCode.CONFLICT, "duplicate row")

    with pytest.raises(StoreError):
        asyncio.run(body())
    assert [line["code"] for line in lines(capsys, "store_write_failed")] == ["store.conflict"]


def test_a_write_that_found_its_row_already_there_says_nothing_needed_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def body() -> list[bool]:
        db = Database(_Pool())  # type: ignore[arg-type]  # a fake pool
        async with db.write("a workspace"):
            first = written(None)  # ON CONFLICT DO NOTHING gave no row back
        async with db.write("a workspace"):
            second = written("ws-1")
        return [first, second]

    assert asyncio.run(body()) == [False, True]
    err = capsys.readouterr().err
    unchanged = [json.loads(raw) for raw in err.splitlines() if '"store_unchanged"' in raw]
    saved = [json.loads(raw) for raw in err.splitlines() if '"store_saved"' in raw]
    assert [line["what"].split(" (")[0] for line in unchanged] == ["Nothing needed writing for a workspace"]
    # The next write starts afresh: one "nothing" never makes a later real write look unchanged.
    assert [line["record"] for line in saved] == ["a workspace"]


def test_an_update_that_matched_no_row_says_nothing_needed_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def body() -> list[bool]:
        db = Database(_Pool())  # type: ignore[arg-type]  # a fake pool
        found = []
        for status in ("UPDATE 0", "UPDATE 1", "INSERT 0 1", "DELETE 0", "UPDATE 2"):
            async with db.write(f"a briefing ({status})"):
                found.append(changed(status))
        return found

    assert asyncio.run(body()) == [False, True, True, False, True]
    err = capsys.readouterr().err
    unchanged = [json.loads(raw)["record"] for raw in err.splitlines() if '"store_unchanged"' in raw]
    assert unchanged == ["a briefing (UPDATE 0)", "a briefing (DELETE 0)"]


def test_nothing_written_inside_a_write_does_not_leak_out_of_it(capsys: pytest.CaptureFixture[str]) -> None:
    async def body() -> None:
        db = Database(_Pool())  # type: ignore[arg-type]  # a fake pool
        async with db.write("an outer record"), db.write("an inner record"):
            written(None)
        async with db.write("a later record"):
            pass

    asyncio.run(body())
    err = capsys.readouterr().err
    unchanged = [json.loads(raw)["record"] for raw in err.splitlines() if '"store_unchanged"' in raw]
    saved = [json.loads(raw)["record"] for raw in err.splitlines() if '"store_saved"' in raw]
    assert (unchanged, saved) == (["an inner record"], ["an outer record", "a later record"])


# --- chat tools ----------------------------------------------------------------------------------


class _Model:
    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)

    async def chat(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float
    ) -> str:
        return self.replies.pop(0)


def _call(name: str, arg: str) -> str:
    return f"```tool\nname: {name}\narg: {arg}\n```"


@pytest.mark.parametrize(
    ("asked", "reason", "tool"),
    [
        (_call("ignore-previous-instructions", "x"), "unknown_tool", None),
        (_call("read-logs", "everything"), "choice_not_allowed", "read-logs"),
    ],
)
def test_a_refused_tool_is_logged_without_echoing_the_models_words(
    capsys: pytest.CaptureFixture[str], asked: str, reason: str, tool: str | None
) -> None:
    async def run(arg: str) -> str:
        return "text"

    tools = [Tool("read-logs", "reads logs", ("eva",), run, trusted=True, label="Logs")]
    chat = FactChat(_Model([asked, "done"]), "local", tools=tools)
    asyncio.run(chat.ask("q", "f"))
    [line] = lines(capsys, "chat_tool_refused")
    assert (line["level"], line["reason"], line.get("tool")) == ("warning", reason, tool)
    assert "ignore-previous" not in json.dumps(line)
    assert "everything" not in line["what"]


# --- settings ------------------------------------------------------------------------------------


def test_a_settings_list_says_it_was_loaded_or_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "uptime.yaml"
    good.write_text("eva:\n  - {url: 'https://eva.example.com', expect: ok}\n", encoding="utf-8")
    assert list(uptime_list(good, {"eva"})) == ["eva"]
    [line] = lines(capsys, "settings_loaded")
    assert (line["setting"], line["entries"]) == ("uptime", 1)

    bad = tmp_path / "broken.yaml"
    bad.write_text("eva: [\n", encoding="utf-8")
    with pytest.raises(ListNotUsableError):
        uptime_list(bad, {"eva"})
    [line] = lines(capsys, "settings_refused")
    assert (line["level"], line["setting"]) == ("error", "uptime")
    assert str(tmp_path) not in line["what"]  # the path stays on the console
