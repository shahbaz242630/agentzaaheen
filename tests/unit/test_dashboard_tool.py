from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures import paddle_world as world
from tools import chat_tools, dashboard, dashboard_sources


@pytest.fixture
def ui(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path


def test_serves_on_the_laptop_only(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, int]] = []

    def fake_serve(app: FastAPI, host: str, port: int) -> None:
        calls.append((host, port))

    assert dashboard.main([], static_dir=ui, serve=fake_serve) == 0
    assert calls == [("127.0.0.1", 8765)]  # listens on this laptop only
    assert "open http://localhost:8765" in capsys.readouterr().out  # passkeys need a name, not an IP


def test_takes_another_port(ui: Path) -> None:
    calls: list[int] = []
    assert dashboard.main(["--port", "9000"], static_dir=ui, serve=lambda _a, _h, p: calls.append(p)) == 0
    assert calls == [9000]


@pytest.mark.parametrize("port", ["80", "70000"])
def test_refuses_odd_ports(ui: Path, port: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert dashboard.main(["--port", port], static_dir=ui, serve=lambda *_: None) == 1
    assert "1024 and 65535" in capsys.readouterr().out


def test_says_how_to_build_the_ui_when_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert dashboard.main([], static_dir=tmp_path, serve=lambda *_: None) == 1
    assert "npm run build" in capsys.readouterr().out


def test_has_no_host_option() -> None:
    with pytest.raises(SystemExit):
        dashboard.main(["--host", "0.0.0.0"], serve=lambda *_: None)  # noqa: S104


def test_the_ui_contract_fixture_matches_the_api() -> None:
    # The UI's tests check this file against its zod shapes, so both sides agree.
    on_disk = dashboard.CONTRACT_FIXTURE.read_text(encoding="utf-8")
    assert on_disk == dashboard.contract_sample(), "run: uv run python -m tools.dashboard --export-contract"


def test_exports_the_contract_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "apps" / "dashboard" / "src" / "contract" / "sample.json"
    monkeypatch.setattr(dashboard, "UI_ROOT", tmp_path / "apps" / "dashboard")
    monkeypatch.setattr(dashboard, "CONTRACT_FIXTURE", target)
    assert dashboard.main(["--export-contract"], serve=lambda *_: None) == 0
    assert target.read_text(encoding="utf-8") == dashboard.contract_sample()


# --- live Paddle mode (a pretend Paddle; no real request is ever made) ---

FAKE_KEY = "pdl_live_apikey_" + "q" * 20  # built here, never a real key


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _live(
    ui: Path, transport: httpx.AsyncBaseTransport, environ: dict[str, str], clock: Clock
) -> tuple[int, list[FastAPI]]:
    apps: list[FastAPI] = []
    code = dashboard.main(
        ["--paddle-key-env", "K"],
        static_dir=ui,
        serve=lambda app, _h, _p: apps.append(app),
        environ=environ,
        transport=transport,
        clock=clock,
    )
    return code, apps


def _paddle_card(app: FastAPI) -> dict[str, object]:
    detail = TestClient(app, base_url="http://127.0.0.1").get("/api/projects/zaaheen").json()
    return next(card for card in detail["cards"] if card["id"] == "paddle")


def test_live_mode_shows_real_paddle_numbers_and_never_the_key(
    ui: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paddle = world.PaddleWorld()
    clock = Clock(world.NOW + timedelta(minutes=1))
    code, apps = _live(ui, httpx.MockTransport(paddle.serve), {"K": FAKE_KEY}, clock)
    assert code == 0
    out = capsys.readouterr().out
    assert "read 7 subscriptions, 5 payment attempts and 4 refunds" in out
    assert "real numbers from Paddle for Zaaheen" in out
    assert FAKE_KEY not in out
    assert {r.url.host for r in paddle.requests} <= {"api.paddle.com", "sandbox-api.paddle.com"}
    assert paddle.requests[0].url.host == "api.paddle.com"
    assert all(r.method == "GET" for r in paddle.requests)

    card = _paddle_card(apps[0])
    assert card["sample"] is False
    assert card["freshness"] == "fresh"
    assert card["source"] == "Paddle (live)"
    values = {m["label"]: m["value"] for m in card["metrics"]}  # type: ignore[attr-defined]
    assert values["MRR (USD)"] == {"kind": "money", "minor": 1300, "currency": "USD"}
    assert values["Active subscribers"] == {"kind": "count", "value": 3}


def test_live_numbers_turn_unknown_once_they_are_old(ui: Path) -> None:
    clock = Clock(world.NOW)
    code, apps = _live(ui, httpx.MockTransport(world.PaddleWorld().serve), {"K": FAKE_KEY}, clock)
    assert code == 0
    clock.now = world.NOW + dashboard_sources.PADDLE_SCHEDULE * 3
    card = _paddle_card(apps[0])
    assert card["freshness"] == "stale"
    assert all(m["value"]["kind"] == "unknown" for m in card["metrics"])  # type: ignore[attr-defined]


def test_a_refused_key_explains_itself_and_does_not_start(
    ui: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, apps = _live(
        ui, httpx.MockTransport(lambda r: httpx.Response(403)), {"K": FAKE_KEY}, Clock(world.NOW)
    )
    assert code == 1
    assert apps == []
    out = capsys.readouterr().out
    assert "failed with net.auth at GET /subscriptions" in out
    assert "What to do: Create a new read-only API key in Paddle" in out
    assert FAKE_KEY not in out


@pytest.mark.parametrize(
    ("environ", "words"),
    [({}, "K is not set"), ({"K": "pdl live key"}, "spaces or control characters")],
)
def test_a_missing_or_mangled_key_is_refused_before_any_request(
    ui: Path, environ: dict[str, str], words: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    code, apps = _live(ui, httpx.MockTransport(serve), environ, Clock(world.NOW))
    assert code == 1
    assert apps == []
    assert words in capsys.readouterr().out


def test_the_month_starts_at_midnight_utc_on_the_first() -> None:
    assert dashboard_sources.month_start(datetime(2026, 10, 18, 12, 30, tzinfo=UTC)) == datetime(
        2026, 10, 1, tzinfo=UTC
    )


# ---------------------------------------------------------------- the chat's model (F05)


def test_the_chat_answers_through_the_model_server(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[httpx.Request] = []

    def model_server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "MRR is USD 1240.00 (sample)."}}]}
        )

    apps: list[FastAPI] = []
    code = dashboard.main(
        ["--model-url", "http://127.0.0.1:8766"],
        static_dir=ui,
        serve=lambda app, _h, _p: apps.append(app),
        transport=httpx.MockTransport(model_server),
    )
    assert code == 0
    assert "chat connected to the local model" in capsys.readouterr().out
    client = TestClient(apps[0], base_url="http://127.0.0.1")
    models = client.get("/api/models").json()["models"]
    assert [m["id"] for m in models if m["available"]] == [chat_tools.LOCAL_MODEL_ID]
    response = client.post(
        "/api/chat",
        json={"project_id": "zaaheen", "model_id": chat_tools.LOCAL_MODEL_ID, "question": "How is revenue?"},
        headers={"X-Yaqzan-Request": "1"},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "MRR is USD 1240.00 (sample)."
    assert str(seen[0].url) == "http://127.0.0.1:8766/v1/chat/completions"
    assert b"How is revenue?" in seen[0].content


def test_a_stopped_model_server_is_reported_not_hidden(ui: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    apps: list[FastAPI] = []
    dashboard.main(
        ["--model-url", "http://127.0.0.1:8766"],
        static_dir=ui,
        serve=lambda app, _h, _p: apps.append(app),
        transport=httpx.MockTransport(down),
    )
    response = TestClient(apps[0], base_url="http://127.0.0.1").post(
        "/api/chat",
        json={"model_id": chat_tools.LOCAL_MODEL_ID, "question": "hi"},
        headers={"X-Yaqzan-Request": "1"},
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "net.unavailable"}


@pytest.mark.parametrize(
    "url", ["http://10.0.0.5:8766", "http://localhost:8766", "https://models.example.com"]
)
def test_a_model_address_off_the_laptop_is_refused(
    ui: Path, url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[FastAPI] = []
    assert dashboard.main(["--model-url", url], static_dir=ui, serve=lambda a, *_: served.append(a)) == 1
    assert served == []
    assert "net.blocked" in capsys.readouterr().out


# ---------------------------------------------------------------- the Customize page (F06)


def test_the_customize_page_lists_what_is_on_disk(ui: Path) -> None:
    apps: list[FastAPI] = []
    assert dashboard.main([], static_dir=ui, serve=lambda app, *_: apps.append(app)) == 0
    page = TestClient(apps[0], base_url="http://127.0.0.1").get("/api/registry").json()
    assert [c["id"] for c in page["connectors"]] == ["github", "paddle"]
    assert {u["status"] for c in page["connectors"] for u in c["uses"]} == {"needs_key"}
    assert [u["project_id"] for u in page["connectors"][1]["uses"]] == ["zaaheen"]
    assert [s["id"] for s in page["skills"]] == ["triage-failing-build"]


def test_bundles_are_shown_and_bad_ones_explained(
    ui: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "bundles" / "kit" / ".claude-plugin"
    bad = tmp_path / "bundles" / "broken" / ".codex-plugin"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "plugin.json").write_text('{"name": "kit"}', encoding="utf-8")
    (bad / "plugin.json").write_text('{"name": "Broken"}', encoding="utf-8")
    apps: list[FastAPI] = []
    argv = ["--bundles", str(tmp_path / "bundles")]
    assert dashboard.main(argv, static_dir=ui, serve=lambda app, *_: apps.append(app)) == 0
    page = TestClient(apps[0], base_url="http://127.0.0.1").get("/api/registry").json()
    assert [p["id"] for p in page["plugins"]] == ["kit"]
    assert [p["path"] for p in page["problems"]] == ["bundles/broken"]
    assert "the plugin at bundles/broken was not loaded" in capsys.readouterr().out
