from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from yaqzan.core.sdk import (
    MAX_LOG_CHARS,
    MAX_LOG_LINES,
    FailedStepLog,
    clean_log,
    log_digest,
    make_failed_step_log,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SHA = "a" * 40


def raw(*lines: str) -> bytes:
    return "\n".join(lines).encode("utf-8")


def test_timestamps_colours_and_control_characters_are_removed() -> None:
    lines, truncated = clean_log(
        raw("2026-10-19T08:12:33.1234567Z \x1b[36;1mnpm test\x1b[0m", "2026-10-19T08:12:34Z ok\x07\x00")
    )
    assert lines == ("npm test", "ok")
    assert truncated is False


def test_it_ends_at_the_first_error_and_says_what_was_left_out() -> None:
    lines, truncated = clean_log(raw("setup", "##[error]first failure", "##[error]second", "cleanup"))
    assert lines == ("setup", "##[error]first failure")
    assert truncated is True


def test_without_an_error_mark_it_keeps_the_end() -> None:
    lines, truncated = clean_log(raw(*(f"line {i}" for i in range(500))))
    assert len(lines) == MAX_LOG_LINES
    assert lines[-1] == "line 499"
    assert truncated is True


def test_only_the_lines_before_the_error_are_kept() -> None:
    lines, _ = clean_log(raw(*(f"line {i}" for i in range(400)), "##[error]boom", "after"))
    assert len(lines) == MAX_LOG_LINES
    assert lines[-1] == "##[error]boom"
    assert lines[0] == "line 201"


def test_secrets_and_emails_are_blanked() -> None:
    token = "ghp_" + "Z" * 36
    lines, _ = clean_log(
        raw(f"export TOKEN={token}", "contact ops@example.com", "Authorization: Bearer abcdefghijkl")
    )
    text = "\n".join(lines)
    assert token not in text
    assert "ops@example.com" not in text
    assert "abcdefghijkl" not in text


def test_long_lines_and_long_logs_are_capped() -> None:
    lines, truncated = clean_log(raw(*("x" * 1000 for _ in range(100))))
    assert all(len(line) <= 400 for line in lines)
    assert sum(len(line) for line in lines) <= MAX_LOG_CHARS
    assert truncated is True


def test_an_empty_or_undecodable_log_is_handled() -> None:
    assert clean_log(b"") == ((), False)
    assert clean_log(b"\xff\xfe broken \xc3")[0] == ("�� broken �",)


def test_the_shape_is_built_redacted_and_digested() -> None:
    log = make_failed_step_log(
        source="github",
        repo="o/r",
        run_id=7,
        commit=SHA,
        job="test (token ghp_" + "Q" * 36 + ")",
        step="\x00Run tests",
        raw=raw("##[error]boom"),
        finished_at=None,
        fetched_at=NOW,
    )
    assert "ghp_" not in log.job
    assert log.step == "Run tests"
    assert log.digest == log_digest(("##[error]boom",))


def test_an_unnamed_job_still_has_a_name() -> None:
    log = make_failed_step_log(
        source="github",
        repo="o/r",
        run_id=1,
        commit=SHA,
        job="  ",
        step=None,
        raw=b"",
        finished_at=None,
        fetched_at=NOW,
    )
    assert (log.job, log.step) == ("unnamed job", None)


@pytest.mark.parametrize(
    "change",
    [
        {"digest": "b" * 64},
        {"lines": ("x" * 401,), "digest": log_digest(("x" * 401,))},
        {"lines": ("x" * 400,) * 41, "digest": log_digest(("x" * 400,) * 41)},
    ],
)
def test_the_shape_refuses_a_wrong_digest_or_oversized_text(change: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "source": "github",
        "repo": "o/r",
        "run_id": 1,
        "commit": SHA,
        "job": "test",
        "lines": ("a",),
        "truncated": False,
        "digest": log_digest(("a",)),
        "fetched_at": NOW,
    }
    FailedStepLog.model_validate(fields)
    with pytest.raises(ValidationError):
        FailedStepLog.model_validate({**fields, **change})
