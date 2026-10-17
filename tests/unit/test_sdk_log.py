"""The redacting logger, including the privacy canary (test ID ``log/privacy_canary``).

Fake secrets are assembled at runtime so the secret scanner never sees a
token-shaped string in this file.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretBytes, SecretStr

from yaqzan.core.sdk import REDACTED, contains_sensitive, get_logger, redact, redact_text
from yaqzan.core.sdk.log import is_sensitive_key


def _fake(prefix: str, body_char: str, length: int) -> str:
    return prefix + body_char * length


CANARIES: dict[str, str] = {
    "github": _fake("gh" + "p_", "A", 36),
    "github_pat": _fake("github" + "_pat_", "B", 40),
    "stripe": _fake("sk" + "_live_", "C", 24),
    "stripe_webhook": _fake("wh" + "sec_", "D", 32),
    "llm": _fake("sk" + "-", "E", 40),
    "aws": "AK" + "IA" + "Q" * 16,
    "slack": _fake("xo" + "xb-", "1", 20),
    "telegram": "123456789:" + "F" * 35,
    "jwt": "ey" + "J" + "a" * 10 + ".ey" + "J" + "b" * 10 + "." + "c" * 20,
    "pem": "-----BEGIN " + "PRIVATE KEY-----\nMIIBVQIBADANBg\n-----END " + "PRIVATE KEY-----",
}
CANARY_MARKERS = {
    "github": "A" * 36,
    "github_pat": "B" * 40,
    "stripe": "C" * 24,
    "stripe_webhook": "D" * 32,
    "llm": "E" * 40,
    "aws": "Q" * 16,
    "slack": "1" * 20,
    "telegram": "F" * 35,
    "jwt": "c" * 20,
    "pem": "MIIBVQIBADANBg",
}
CARD = "4111 1111 1111 1111"  # the standard public test card number (passes Luhn)


def _logged_lines(emit: Any) -> str:
    sink = io.StringIO()
    emit(get_logger("test", sink=sink))
    return sink.getvalue()


@pytest.mark.parametrize("name", sorted(CANARIES))
def test_secret_shapes_are_removed_from_free_text(name: str) -> None:
    text = f"request failed with credential {CANARIES[name]} attached"
    cleaned = redact_text(text)
    assert CANARY_MARKERS[name] not in cleaned
    assert REDACTED in cleaned
    assert contains_sensitive(text)


def test_privacy_canary_nothing_secret_reaches_the_log() -> None:
    all_secrets = " ".join(CANARIES.values())

    def emit(log: Any) -> None:
        log.info(f"calling vendor with {CANARIES['github']}")
        log.warning("vendor said no", response={"echo": all_secrets, "nested": [CANARIES["stripe"]]})
        log.info("login", password="hunter2-plain-words", api_key="plainvalue", authorization="Basic xyz")
        log.info("secret objects", value=SecretStr("s3cr3t-value"), raw=SecretBytes(b"bytes-value"))
        log.info("customer wrote", sender="jane.doe@example.com", card=CARD)
        log.info("callback", url="https://api.example.com/cb?token=abc123def&page=2")
        log.info("db", dsn="postgres://admin:pa55word@db.internal:5432/yaqzan")
        try:
            raise RuntimeError(f"upstream rejected {CANARIES['llm']}")
        except RuntimeError:
            log.exception("boom")

    output = _logged_lines(emit)
    for marker in CANARY_MARKERS.values():
        assert marker not in output
    for plain in (
        "hunter2",
        "plainvalue",
        "s3cr3t",
        "bytes-value",
        "jane.doe",
        "1111 1111",
        "abc123def",
        "pa55word",
    ):
        assert plain not in output
    assert "page=2" in output  # harmless parts survive


def test_every_line_is_json_with_level_time_and_component() -> None:
    output = _logged_lines(lambda log: log.info("hello", count=3))
    line = json.loads(output.strip())
    assert line["event"] == "hello"
    assert line["level"] == "info"
    assert line["component"] == "test"
    assert line["count"] == 3
    assert line["timestamp"].endswith("Z")


def test_bound_context_appears_and_is_redacted_too() -> None:
    sink = io.StringIO()
    get_logger("svc", sink=sink, correlation_id="c-1", session="should-hide").info("x")
    line = json.loads(sink.getvalue())
    assert line["correlation_id"] == "c-1"
    assert line["session"] == REDACTED


def test_logger_defaults_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("svc").info("to stderr")
    assert "to stderr" in capsys.readouterr().err


@pytest.mark.parametrize(
    "key", ["password", "DB_PASSWORD", "apiKey", "x-api-key", "Authorization", "refresh_token", "dek"]
)
def test_sensitive_key_names(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["count", "project_id", "event", "status"])
def test_ordinary_key_names(key: str) -> None:
    assert not is_sensitive_key(key)


def test_numbers_under_sensitive_keys_are_kept() -> None:
    assert redact({"tokens_in": 120, "token_ok": True, "token": None}) == {
        "tokens_in": 120,
        "token_ok": True,
        "token": None,
    }


def test_structures_are_copied_not_mutated() -> None:
    original = {"password": "x", "items": ("a", {"secret": "y"})}
    cleaned = redact(original)
    assert original == {"password": "x", "items": ("a", {"secret": "y"})}
    assert cleaned == {"password": REDACTED, "items": ["a", {"secret": REDACTED}]}


def test_bytes_and_unknown_objects() -> None:
    class Thing:
        def __repr__(self) -> str:
            return "Thing(owner=a@b.co)"

    assert redact(b"\x00\x01") == "<2 bytes>"
    assert redact(Thing()) == "Thing(owner=[EMAIL])"
    assert redact(1.5) == 1.5


def test_deep_nesting_is_truncated() -> None:
    value: Any = "leaf"
    for _ in range(20):
        value = [value]
    flat = json.dumps(redact(value))
    assert "[TRUNCATED]" in flat
    assert "leaf" not in flat


def test_card_needs_luhn_so_ordinary_numbers_survive() -> None:
    assert redact_text(f"card {CARD}") == "card [CARD]"
    assert redact_text("order 4111111111111112 total") == "order 4111111111111112 total"


def test_bearer_header_is_cut() -> None:
    header = "Authorization: " + "Bear" + "er " + _fake("", "z", 24)
    assert redact_text(header) == "Authorization: Bearer " + REDACTED


def test_plain_sentences_are_untouched() -> None:
    text = "Uptime check passed for 3 sites in 120 ms; next run at 09:15."
    assert redact_text(text) == text
    assert not contains_sensitive(text)


@given(st.text(max_size=200))
def test_redaction_is_idempotent(text: str) -> None:
    once = redact_text(text)
    assert redact_text(once) == once


def test_hostile_long_input_is_cut_and_stays_fast() -> None:
    import time  # noqa: PLC0415  # only this test needs it

    blob = "a" * 200_000
    started = time.perf_counter()
    cleaned = redact_text(blob)
    assert time.perf_counter() - started < 0.5
    assert len(cleaned) < 8_300
    assert cleaned.endswith("chars]")


def test_secrets_used_as_dict_keys_are_removed() -> None:
    cleaned = redact({"jane.doe@example.com": "subscribed"})
    assert cleaned == {"[EMAIL]": "subscribed"}


def test_numeric_secrets_are_hidden_but_counts_stay() -> None:
    assert redact({"otp": 123456, "pin_code_secret": 1234}) == {"otp": REDACTED, "pin_code_secret": REDACTED}
    assert redact({"tokens_out": 50, "token_count": 7}) == {"tokens_out": 50, "token_count": 7}
