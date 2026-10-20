"""The local model launcher: loopback only, refuses when memory is short, always stops its server.

No real process and no real model: the process, memory reading and health check are fakes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tools import local_model

GIB = 1024**3


class FakeProcess:
    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - terminate always works in these tests
        self.terminated = True


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x" * 1000)
    return server, model


def run(
    files: tuple[Path, Path],
    *,
    free: int | None = 8 * GIB,
    process: FakeProcess | None = None,
    health: list[int] | None = None,
    extra: list[str] | None = None,
) -> tuple[int, list[list[str]], FakeProcess]:
    server, model = files
    started: list[list[str]] = []
    proc = process or FakeProcess()
    answers = iter(health or [200])

    def start(command: list[str]) -> subprocess.Popen[bytes]:
        started.append(command)
        return proc  # type: ignore[return-value]  # a stand-in with the methods used

    def get(url: str) -> int:
        assert url == "http://127.0.0.1:8766/health"
        status = next(answers)
        if status == 0:
            raise httpx.ConnectError("not yet")
        return status

    code = local_model.main(
        ["--server", str(server), "--model", str(model), *(extra or [])],
        free_memory=lambda: free,
        start=start,
        get=get,
        timeout_s=5.0,
        sleep=lambda _s: None,
        block=lambda _p: None,
    )
    return code, started, proc


def test_starts_on_loopback_without_a_web_page_and_stops_after(
    files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, started, proc = run(files, health=[0, 503, 200])
    assert code == 0
    command = started[0]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8766"
    assert "--no-webui" in command
    assert command[command.index("--parallel") + 1] == "1"
    assert command[command.index("--reasoning") + 1] == "off"
    assert proc.terminated
    assert "ready at http://127.0.0.1:8766" in capsys.readouterr().out


def test_refuses_when_memory_is_short_and_starts_nothing(
    files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, started, _ = run(files, free=1 * GIB)
    assert code == 1
    assert started == []
    out = capsys.readouterr().out
    assert "only 1.0 GB of memory is free" in out
    assert "other work is not stopped" in out


def test_refuses_when_free_memory_cannot_be_read(files: tuple[Path, Path]) -> None:
    code, started, _ = run(files, free=None)
    assert (code, started) == (1, [])


def test_a_server_that_dies_while_loading_is_reported(
    files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(files, process=FakeProcess(exit_code=1), health=[0])
    assert code == 1
    assert "did not become ready" in capsys.readouterr().out


def test_ctrl_c_stops_the_server(files: tuple[Path, Path]) -> None:
    server, model = files
    proc = FakeProcess()

    def interrupted(_p: object) -> None:
        raise KeyboardInterrupt

    code = local_model.main(
        ["--server", str(server), "--model", str(model)],
        free_memory=lambda: 8 * GIB,
        start=lambda _c: proc,  # type: ignore[arg-type,return-value]  # a stand-in with the methods used
        get=lambda _u: 200,
        block=interrupted,
    )
    assert code == 0
    assert proc.terminated


def test_missing_files_are_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def never(_c: list[str]) -> subprocess.Popen[bytes]:
        raise AssertionError("must not start")

    code = local_model.main(
        ["--server", str(tmp_path / "nope.exe"), "--model", str(tmp_path / "m.gguf")],
        free_memory=lambda: 8 * GIB,
        start=never,
    )
    assert code == 1
    assert "no llama-server at" in capsys.readouterr().out


@pytest.mark.parametrize("port", ["80", "70000"])
def test_refuses_odd_ports(files: tuple[Path, Path], port: str) -> None:
    code, started, _ = run(files, extra=["--port", port])
    assert (code, started) == (1, [])


def test_reads_free_memory_on_this_machine() -> None:
    free = local_model.free_memory_bytes()
    assert free is None or free > 0


def test_health_polling_gives_up_at_the_deadline() -> None:
    ready = local_model.wait_until_ready(
        8766,
        FakeProcess(),  # type: ignore[arg-type]  # a stand-in with the methods used
        get=lambda _u: 503,
        timeout_s=0.0,
        sleep=lambda _s: None,
    )
    assert ready is False


@pytest.mark.parametrize(
    ("meminfo", "expected"),
    [
        ("MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\n", 4000000 * 1024),
        ("MemTotal: 16000000 kB\n", None),
    ],
)
def test_reads_free_memory_on_linux(
    monkeypatch: pytest.MonkeyPatch, meminfo: str, expected: int | None
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", lambda _self, encoding: meminfo)
    assert local_model.free_memory_bytes() == expected


def test_unreadable_meminfo_means_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(_self: Path, encoding: str) -> str:
        raise OSError("no /proc")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert local_model.free_memory_bytes() is None


class StubbornProcess(FakeProcess):
    """Ignores terminate, so only a hard kill stops it."""

    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("llama-server", timeout or 0)

    def kill(self) -> None:
        self.killed = True


def test_a_server_that_ignores_terminate_is_killed() -> None:
    proc = StubbornProcess()
    local_model._stop(proc)  # type: ignore[arg-type]  # a stand-in with the methods used
    assert proc.killed
