"""The dev database tool: it only touches its own container and waits when Docker is short of memory."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tools import dev_db

# Built at runtime so secret scanners don't mistake a test value for a real credential.
FAKE_PW = "-".join(["fake", "value"])


class FakeDocker:
    """Answers docker commands from a script and records what was asked."""

    def __init__(
        self,
        *,
        container: str = "absent",
        total_mb: int = 8000,
        used: Sequence[str] = (),
        ready_after: int = 0,
        free_sequence: Sequence[int] | None = None,
    ) -> None:
        self.container = container
        self.total_mb = total_mb
        self.used = list(used)
        self.ready_after = ready_after
        self.free_sequence = list(free_sequence) if free_sequence else None
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:  # noqa: PLR0911  # one answer per command
        call = list(args)
        self.calls.append(call)
        command = call[0]
        if command == "info":
            if self.free_sequence is not None:
                return 0, str(
                    (self.free_sequence.pop(0) if len(self.free_sequence) > 1 else self.free_sequence[0])
                    * 1024**2
                )
            return 0, str(self.total_mb * 1024**2)
        if command == "stats":
            return 0, "\n".join(self.used)
        if command == "inspect":
            return self._inspect(call)
        if command == "exec":
            self.ready_after -= 1
            return (0 if self.ready_after < 0 else 1), ""
        if command in {"run", "start"}:
            self.container = "running"
            return 0, "abc123"
        if command == "rm":
            self.container = "absent"
            return 0, ""
        return 1, ""

    def _inspect(self, call: list[str]) -> tuple[int, str]:
        if self.container == "absent":
            return 1, ""
        if "{{json .Config.Env}}" in call:
            return 0, '["PATH=/usr/bin","POSTGRES_' + "PASSWORD=" + FAKE_PW + '"]'
        label = "someone-else" if self.container == "foreign" else dev_db.LABEL_VALUE
        return 0, f"{label}|{'true' if self.container == 'running' else 'false'}"

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


def no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512MiB", 512 * 1024**2),
        ("1.5GiB", int(1.5 * 1024**3)),
        ("80kB", 80_000),
        ("0B", 0),
        (" 3MB ", 3_000_000),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert dev_db.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "12", "5 parsecs", "MiB"])
def test_parse_size_refuses_nonsense(text: str) -> None:
    with pytest.raises(ValueError, match="size"):
        dev_db.parse_size(text)


def test_free_memory_subtracts_every_running_container() -> None:
    docker = FakeDocker(total_mb=4096, used=["1GiB / 7GiB", "512MiB / 7GiB"])
    assert dev_db.free_mb(docker) == 4096 - 1024 - 512


@pytest.mark.parametrize(
    "answers",
    [
        {"info": (1, "")},
        {"info": (0, "lots")},
        {"info": (0, "100"), "stats": (1, "")},
        {"info": (0, "100"), "stats": (0, "garbage / x")},
    ],
)
def test_free_memory_unknown_when_docker_cannot_say(answers: dict[str, tuple[int, str]]) -> None:
    def docker(args: Sequence[str]) -> tuple[int, str]:
        return answers[args[0]]

    assert dev_db.free_mb(docker) is None


def test_up_starts_a_hardened_local_only_container() -> None:
    docker = FakeDocker(ready_after=2)
    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_OK
    run_call = next(c for c in docker.calls if c[0] == "run")
    joined = " ".join(run_call)
    assert f"--publish {dev_db.HOST}:{dev_db.PORT}:5432" in joined
    assert f"--label {dev_db.LABEL_KEY}={dev_db.LABEL_VALUE}" in joined
    assert f"--memory {dev_db.MEMORY_LIMIT_MB}m" in joined
    for flag in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 999:999"):
        assert flag in joined
    assert "@sha256:" in dev_db.IMAGE


def test_up_uses_a_fresh_random_password_each_time() -> None:
    first, second = FakeDocker(), FakeDocker()
    dev_db.up(first, no_sleep)
    dev_db.up(second, no_sleep)

    def password(docker: FakeDocker) -> str:
        run_call = next(c for c in docker.calls if c[0] == "run")
        return next(a for a in run_call if a.startswith("POSTGRES_PASSWORD="))

    assert password(first) != password(second)


def test_up_reuses_a_running_container() -> None:
    docker = FakeDocker(container="running")
    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_OK
    assert "run" not in docker.commands()


def test_up_restarts_a_stopped_container() -> None:
    docker = FakeDocker(container="stopped")
    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_OK
    assert "start" in docker.commands()
    assert "run" not in docker.commands()


def test_up_refuses_when_memory_is_low() -> None:
    docker = FakeDocker(total_mb=dev_db.MIN_FREE_MB + 100, used=["200MiB / 2GiB"])
    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_LOW_MEMORY
    assert "run" not in docker.commands()


def test_up_refuses_when_memory_cannot_be_read() -> None:
    def docker(args: Sequence[str]) -> tuple[int, str]:
        del args
        return 1, ""

    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_LOW_MEMORY


def test_up_waits_for_memory_then_starts() -> None:
    slept: list[float] = []
    docker = FakeDocker(free_sequence=[100, 100, 4000])
    assert dev_db.up(docker, slept.append, wait_minutes=30) == dev_db.EXIT_OK
    assert slept[:2] == [dev_db.RETRY_SECONDS, dev_db.RETRY_SECONDS]
    assert "run" in docker.commands()


def test_up_gives_up_after_the_wait() -> None:
    slept: list[float] = []
    docker = FakeDocker(free_sequence=[100])
    assert dev_db.up(docker, slept.append, wait_minutes=10) == dev_db.EXIT_LOW_MEMORY
    assert slept == [dev_db.RETRY_SECONDS, dev_db.RETRY_SECONDS]


def test_up_reports_a_database_that_never_becomes_ready() -> None:
    docker = FakeDocker(ready_after=10_000)
    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_ERROR


def test_up_reports_a_failed_docker_run() -> None:
    def docker(args: Sequence[str]) -> tuple[int, str]:
        if args[0] == "info":
            return 0, str(8000 * 1024**2)
        if args[0] == "stats":
            return 0, ""
        return 1, ""

    assert dev_db.up(docker, no_sleep) == dev_db.EXIT_ERROR


@pytest.mark.parametrize("action", ["up", "down"])
def test_never_touches_a_container_that_is_not_ours(action: str) -> None:
    docker = FakeDocker(container="foreign")
    result = dev_db.up(docker, no_sleep) if action == "up" else dev_db.down(docker)
    assert result == dev_db.EXIT_ERROR
    assert not {"run", "start", "rm", "stop"} & set(docker.commands())


def test_down_removes_only_our_container() -> None:
    docker = FakeDocker(container="running")
    assert dev_db.down(docker) == dev_db.EXIT_OK
    assert ["rm", "--force", dev_db.NAME] in docker.calls


def test_down_when_absent_is_fine() -> None:
    docker = FakeDocker()
    assert dev_db.down(docker) == dev_db.EXIT_OK
    assert "rm" not in docker.commands()


def test_down_reports_a_failed_remove() -> None:
    def docker(args: Sequence[str]) -> tuple[int, str]:
        if args[0] == "inspect":
            return 0, f"{dev_db.LABEL_VALUE}|true"
        return 1, ""

    assert dev_db.down(docker) == dev_db.EXIT_ERROR


def test_admin_url_comes_from_our_running_container_only() -> None:
    assert dev_db.admin_url(FakeDocker(container="running")) == (
        f"postgresql://postgres:{FAKE_PW}@{dev_db.HOST}:{dev_db.PORT}/postgres"
    )
    assert dev_db.admin_url(FakeDocker(container="stopped")) is None
    assert dev_db.admin_url(FakeDocker(container="foreign")) is None


def test_admin_url_none_when_password_missing() -> None:
    def docker(args: Sequence[str]) -> tuple[int, str]:
        if "{{json .Config.Env}}" in args:
            return 0, '["PATH=/usr/bin"]'
        return 0, f"{dev_db.LABEL_VALUE}|true"

    assert dev_db.admin_url(docker) is None


def test_main_dispatches(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[str] = []

    def fake_up(*, wait_minutes: int) -> int:
        seen.append(f"up {wait_minutes}")
        return 0

    def fake_down() -> int:
        seen.append("down")
        return 0

    monkeypatch.setattr(dev_db, "up", fake_up)
    monkeypatch.setattr(dev_db, "down", fake_down)
    monkeypatch.setattr(dev_db, "state", lambda _run: "absent")
    assert dev_db.main(["up", "--wait-minutes", "15"]) == 0
    assert dev_db.main(["down"]) == 0
    assert dev_db.main(["status"]) == 0
    assert seen == ["up 15", "down"]
    assert "dev-db: absent" in capsys.readouterr().out
