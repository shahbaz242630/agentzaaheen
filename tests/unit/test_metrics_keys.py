"""The key-expiry rule: P2 within 14 days, P1 once expired or refused, P3 when no date is known."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.metrics import WARN_BEFORE, KeyHealth, days_left, key_health
from agentzaaheen.core.sdk import Severity

NOW = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
LIVE_KEY_ENDS = datetime(2027, 1, 16, 9, 0, tzinfo=UTC)  # the shape of the Chairman's live Paddle key


@pytest.mark.parametrize(
    ("left", "expected"),
    [
        (WARN_BEFORE + timedelta(seconds=1), KeyHealth.OK),
        (WARN_BEFORE, KeyHealth.EXPIRING),
        (timedelta(seconds=1), KeyHealth.EXPIRING),
        (timedelta(0), KeyHealth.EXPIRED),
        (-timedelta(days=3), KeyHealth.EXPIRED),
    ],
)
def test_the_date_decides_when_nothing_was_refused(left: timedelta, expected: KeyHealth) -> None:
    assert key_health(NOW + left, NOW, last_error_code=None) is expected


@pytest.mark.parametrize("code", ["net.auth", "source.auth"])
def test_a_refused_key_is_dead_whatever_its_date(code: str) -> None:
    assert key_health(NOW + timedelta(days=60), NOW, last_error_code=code) is KeyHealth.REFUSED
    assert key_health(None, NOW, last_error_code=code) is KeyHealth.REFUSED


def test_other_errors_do_not_condemn_the_key() -> None:
    assert key_health(NOW + timedelta(days=60), NOW, last_error_code="net.timeout") is KeyHealth.OK


def test_no_recorded_date_is_a_reminder() -> None:
    assert key_health(None, NOW, last_error_code=None) is KeyHealth.NO_DATE


def test_the_live_key_first_warns_on_2_january() -> None:
    day_before = NOW - timedelta(days=1)
    assert key_health(LIVE_KEY_ENDS, day_before, last_error_code=None) is KeyHealth.OK
    assert key_health(LIVE_KEY_ENDS, NOW, last_error_code=None) is KeyHealth.EXPIRING
    assert days_left(LIVE_KEY_ENDS, NOW) == 14


def test_severity_and_code_per_state() -> None:
    assert {h: h.severity for h in KeyHealth} == {
        KeyHealth.OK: None,
        KeyHealth.EXPIRING: Severity.P2,
        KeyHealth.EXPIRED: Severity.P1,
        KeyHealth.REFUSED: Severity.P1,
        KeyHealth.NO_DATE: Severity.P3,
    }
    assert KeyHealth.EXPIRING.code == "credential.expiring"


def test_days_left_counts_whole_days() -> None:
    assert days_left(NOW + timedelta(hours=23), NOW) == 0
    assert days_left(NOW + timedelta(days=1), NOW) == 1
    assert days_left(NOW - timedelta(hours=1), NOW) == -1


def test_naive_times_are_refused() -> None:
    naive = datetime(2027, 1, 2, 9, 0)  # noqa: DTZ001  # deliberately naive
    with pytest.raises(ValueError, match="timezone"):
        key_health(naive, NOW, last_error_code=None)
    with pytest.raises(ValueError, match="timezone"):
        key_health(None, naive, last_error_code=None)
