from pathlib import Path

import pytest
from fastapi import FastAPI

from tools import dashboard


@pytest.fixture
def ui(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path


def test_serves_on_the_laptop_only(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, int]] = []

    def fake_serve(app: FastAPI, host: str, port: int) -> None:
        calls.append((host, port))

    assert dashboard.main([], static_dir=ui, serve=fake_serve) == 0
    assert calls == [("127.0.0.1", 8765)]
    assert "http://127.0.0.1:8765" in capsys.readouterr().out


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
