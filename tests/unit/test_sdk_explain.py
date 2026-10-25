"""The error catalogue: every code we can raise has plain words, and the words are complete."""

from __future__ import annotations

import pytest

from tools.dashboard_summaries import NO_TOPIC, UNMAKEABLE
from yaqzan.core.broker import BrokerErrorCode
from yaqzan.core.metrics import Freshness, KeyHealth
from yaqzan.core.net import NetErrorCode
from yaqzan.core.scheduler.collect import _CRASHED, _where
from yaqzan.core.sdk import KNOWN_CODES, SourceError, SourceErrorCode, explain
from yaqzan.core.store import StoreError, StoreErrorCode
from yaqzan.core.vault import VaultErrorCode

RAISED = sorted(
    {c.value for c in (*NetErrorCode, *SourceErrorCode, *StoreErrorCode, *VaultErrorCode, *BrokerErrorCode)}
    | {h.code for h in KeyHealth if h.severity is not None}
    | {f"freshness.{f.value}" for f in Freshness if not f.usable}
    | {_CRASHED, "policy.bad_switch_row"}
    | {NO_TOPIC, UNMAKEABLE, "scout.unreadable"}  # the daily summaries (F17 slice 6)
)


def test_every_code_we_raise_is_in_the_catalogue() -> None:
    assert set(RAISED) <= KNOWN_CODES


def test_the_catalogue_has_no_dead_entries() -> None:
    assert set(RAISED) == KNOWN_CODES


@pytest.mark.parametrize("code", RAISED)
@pytest.mark.parametrize("vendor", ["paddle", None])
def test_every_entry_reads_as_finished_sentences(code: str, vendor: str | None) -> None:
    said = explain(code, vendor=vendor)
    assert said.code == code
    for part in (said.what, said.why, said.fix):
        assert part[:1].isupper()
        assert part.endswith(".")
        assert "{" not in part


def test_the_vendor_is_named_and_capitalised() -> None:
    said = explain("source.auth", vendor="paddle")
    assert said.what == "Paddle refused our API key."
    assert "Create a new read-only key in Paddle's settings" in said.fix
    assert explain("net.timeout").what == "The service took too long to answer."
    assert explain("source.auth", vendor="GitHub").what == "GitHub refused our API key."
    assert said.line() == f"{said.what} Why: {said.why} What to do: {said.fix}"


def test_an_unknown_code_still_gets_an_answer() -> None:
    said = explain("net.made_up", vendor="paddle")
    assert said.code == "net.made_up"
    assert "no description" in said.what
    assert "Claude" in said.fix


def test_the_reason_code_prefers_the_lower_layers_detail() -> None:
    plain = SourceError(SourceErrorCode.UNAVAILABLE, "down")
    assert plain.reason_code == "source.unavailable"
    precise = SourceError(SourceErrorCode.UNAVAILABLE, "down", detail_code="net.timeout", status=504)
    assert (precise.reason_code, precise.status) == ("net.timeout", 504)


def test_where_it_failed_is_set_once_by_the_innermost_caller() -> None:
    err = SourceError(SourceErrorCode.AUTH, "no").at("GET /subscriptions", 2, 4)
    assert err.at("read paddle data") is err
    assert (err.operation, err.attempt, err.attempts) == ("GET /subscriptions", 2, 4)


def test_a_database_failure_has_no_network_place() -> None:
    assert _where(StoreError(StoreErrorCode.UNAVAILABLE, "down")) == {}
    assert _where(SourceError(SourceErrorCode.AUTH, "no")) == {}
