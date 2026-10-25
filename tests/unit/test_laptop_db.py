"""The laptop database tool: its own container and volume only, hardened, data kept when stopped."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from tools import dev_db, laptop_db

FAKE_PW = "-".join(["fake", "value"])  # built at runtime so secret scanners don't flag it


class FakeDocker:
    """Answers docker commands for the laptop container and records what was asked."""

    def __init__(self, container: str = "absent", *, free_mb: int = 8000, fail: str = "") -> None:
        self.container = container
        self.free_mb = free_mb
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:  # noqa: PLR0911  # one answer per command
        call = list(args)
        self.calls.append(call)
        command = call[0]
        if command == self.fail:
            return 1, ""
        if command == "info":
            return 0, str(self.free_mb * 1024**2)
        if command == "stats":
            return 0, ""
        if command == "inspect":
            return self._inspect(call)
        if command == "exec":
            return 0, ""
        if command in {"run", "start"}:
            self.container = "running"
            return 0, "abc123"
        if command == "stop":
            self.container = "stopped"
            return 0, ""
        return 1, ""

    def _inspect(self, call: list[str]) -> tuple[int, str]:
        if self.container == "absent" or call[-1] != laptop_db.NAME:
            return 1, ""
        if "{{json .Config.Env}}" in call:
            return 0, '["POSTGRES_' + "PASSWORD=" + FAKE_PW + '"]'
        label = "someone-else" if self.container == "foreign" else laptop_db.LABEL_VALUE
        return 0, f"{label}|{'true' if self.container == 'running' else 'false'}"

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


def no_sleep(_seconds: float) -> None:
    return None


def test_up_creates_a_hardened_local_only_container_that_keeps_its_data() -> None:
    docker = FakeDocker()
    assert laptop_db.up(docker, no_sleep) == laptop_db.EXIT_OK
    run = next(c for c in docker.calls if c[0] == "run")
    assert run[run.index("--name") + 1] == laptop_db.NAME
    assert run[run.index("--volume") + 1] == f"{laptop_db.VOLUME}:/var/lib/postgresql/data"
    assert run[run.index("--publish") + 1] == f"127.0.0.1:{laptop_db.PORT}:5432"
    assert run[run.index("--memory") + 1] == f"{dev_db.MEMORY_LIMIT_MB}m"
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--user", "--pids-limit"):
        assert flag in run
    assert "fsync=off" not in " ".join(run)  # this one must survive a crash
    assert laptop_db.PORT != dev_db.PORT


def test_up_restarts_a_stopped_container_so_its_password_still_fits_its_data() -> None:
    docker = FakeDocker("stopped")
    assert laptop_db.up(docker, no_sleep) == laptop_db.EXIT_OK
    assert ["start", laptop_db.NAME] in docker.calls
    assert "run" not in docker.commands()


def test_up_reuses_a_running_container() -> None:
    docker = FakeDocker("running")
    assert laptop_db.up(docker, no_sleep) == laptop_db.EXIT_OK
    assert docker.commands() == ["inspect"]


def test_up_refuses_when_memory_is_low() -> None:
    docker = FakeDocker(free_mb=dev_db.MIN_FREE_MB - 1)
    assert laptop_db.up(docker, no_sleep) == laptop_db.EXIT_LOW_MEMORY
    assert "run" not in docker.commands()


def test_up_reports_a_failed_start(capsys: pytest.CaptureFixture[str]) -> None:
    assert laptop_db.up(FakeDocker(fail="run"), no_sleep) == laptop_db.EXIT_ERROR
    assert "failed to start" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["up", "stop"])
def test_never_touches_a_container_that_is_not_ours(action: str) -> None:
    docker = FakeDocker("foreign")
    result = laptop_db.up(docker, no_sleep) if action == "up" else laptop_db.stop(docker)
    assert result == laptop_db.EXIT_ERROR
    assert docker.commands() == ["inspect"]


def test_stop_keeps_the_container_and_its_data() -> None:
    docker = FakeDocker("running")
    assert laptop_db.stop(docker) == laptop_db.EXIT_OK
    assert ["stop", laptop_db.NAME] in docker.calls
    assert "rm" not in docker.commands()


@pytest.mark.parametrize("container", ["absent", "stopped"])
def test_stop_when_not_running_is_fine(container: str) -> None:
    docker = FakeDocker(container)
    assert laptop_db.stop(docker) == laptop_db.EXIT_OK
    assert docker.commands() == ["inspect"]


def test_stop_reports_a_failure() -> None:
    assert laptop_db.stop(FakeDocker("running", fail="stop")) == laptop_db.EXIT_ERROR


def test_derived_passwords_are_stable_per_role_and_safe_in_sql() -> None:
    owner = laptop_db.derived_password(FAKE_PW, laptop_db.OWNER)
    assert owner == laptop_db.derived_password(FAKE_PW, laptop_db.OWNER)
    assert owner != laptop_db.derived_password(FAKE_PW, laptop_db.APP)
    assert owner != laptop_db.derived_password(FAKE_PW + "x", laptop_db.OWNER)
    assert all(c in "0123456789abcdef" for c in owner)
    assert FAKE_PW not in owner


def test_prepare_is_none_when_the_container_is_not_running() -> None:
    assert asyncio.run(laptop_db.prepare(FakeDocker("stopped"))) is None


def test_main_dispatches(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def record(name: str) -> int:
        calls.append(name)
        return 0

    monkeypatch.setattr(laptop_db, "up", lambda: record("up"))
    monkeypatch.setattr(laptop_db, "stop", lambda: record("stop"))
    monkeypatch.setattr(laptop_db, "_ours", lambda _run: "running")
    assert laptop_db.main(["up"]) == 0
    assert laptop_db.main(["stop"]) == 0
    assert laptop_db.main(["status"]) == 0
    assert calls == ["up", "stop"]
    assert "laptop-db: running" in capsys.readouterr().out


def test_the_laptop_ids_never_change_even_after_the_rename() -> None:
    # Made from fixed text with the old name: a new id would orphan the saved passkey and switches.
    assert str(laptop_db.LAPTOP_TENANT) == "38a6867d-7e01-5e36-b8b8-aec6144d2dbb"
    assert {w.slug: str(w.workspace_id) for w in laptop_db.LAPTOP_WORKSPACES}["zaaheen"] == (
        "5753fb4a-63cd-52de-8681-78d1d78d6e9f"
    )
