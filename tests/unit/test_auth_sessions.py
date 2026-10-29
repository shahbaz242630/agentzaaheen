"""Dashboard sessions (ADR-SEC-010): 3 hours idle, 12 hours at most, 3 at once, kept only as hashes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentzaaheen.core.auth import IDLE_AFTER, LONGEST, MAX_LIVE, SessionCheck, Sessions, SignInWait

START = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def test_a_new_session_is_live_and_an_unknown_or_missing_token_is_not() -> None:
    sessions = Sessions(Clock())
    token = sessions.start().token
    assert sessions.check(token, active=False) is SessionCheck.LIVE
    assert sessions.check(token + "x", active=False) is SessionCheck.NONE
    assert sessions.check(None, active=False) is SessionCheck.NONE
    assert sessions.check("", active=False) is SessionCheck.NONE


def test_only_a_hash_of_the_token_is_kept() -> None:
    sessions = Sessions(Clock())
    token = sessions.start().token
    assert len(token) >= 40  # 32 random bytes
    assert token not in repr(vars(sessions))


def test_idle_for_3_hours_ends_it_and_it_stays_ended() -> None:
    clock = Clock()
    sessions = Sessions(clock)
    token = sessions.start().token
    clock.now += IDLE_AFTER - timedelta(seconds=1)
    assert sessions.check(token, active=False) is SessionCheck.LIVE
    clock.now += timedelta(seconds=1)
    assert sessions.check(token, active=False) is SessionCheck.IDLE
    assert sessions.check(token, active=True) is SessionCheck.NONE


def test_only_a_persons_action_restarts_the_idle_hours() -> None:
    clock = Clock()
    sessions = Sessions(clock)
    token = sessions.start().token
    clock.now += timedelta(hours=2)
    assert sessions.check(token, active=False) is SessionCheck.LIVE  # an automatic call: no restart
    clock.now += timedelta(hours=1)
    assert sessions.check(token, active=True) is SessionCheck.IDLE

    token = sessions.start().token
    clock.now += timedelta(hours=2)
    assert sessions.check(token, active=True) is SessionCheck.LIVE  # a click: 3 more hours from here
    clock.now += timedelta(hours=2)
    assert sessions.check(token, active=False) is SessionCheck.LIVE


def test_12_hours_ends_it_however_busy() -> None:
    clock = Clock()
    sessions = Sessions(clock)
    token = sessions.start().token
    while clock.now - START < LONGEST - timedelta(hours=1):
        clock.now += timedelta(hours=1)
        assert sessions.check(token, active=True) is SessionCheck.LIVE
    clock.now = START + LONGEST
    assert sessions.check(token, active=True) is SessionCheck.LIMIT


def test_a_fourth_session_ends_the_oldest() -> None:
    clock = Clock()
    sessions = Sessions(clock)
    tokens = []
    for _ in range(MAX_LIVE):
        started = sessions.start()
        assert not started.ended_oldest
        tokens.append(started.token)
        clock.now += timedelta(minutes=1)
    newest = sessions.start()
    assert newest.ended_oldest
    assert sessions.check(tokens[0], active=False) is SessionCheck.NONE
    assert all(sessions.check(t, active=False) is SessionCheck.LIVE for t in [*tokens[1:], newest.token])
    assert sessions.live() == MAX_LIVE


def test_signing_out_ends_only_that_session() -> None:
    sessions = Sessions(Clock())
    one, two = sessions.start().token, sessions.start().token
    assert sessions.end(one)
    assert not sessions.end(one)
    assert not sessions.end(None)
    assert sessions.check(one, active=False) is SessionCheck.NONE
    assert sessions.check(two, active=False) is SessionCheck.LIVE


def test_each_token_is_new() -> None:
    sessions = Sessions(Clock())
    assert len({sessions.start().token for _ in range(MAX_LIVE)}) == MAX_LIVE


def test_five_failures_in_10_minutes_make_sign_in_wait_1_minute() -> None:
    clock = Clock()
    wait = SignInWait(clock)
    assert [wait.failed() for _ in range(4)] == [False] * 4
    assert not wait.waiting()
    assert wait.failed()
    assert wait.waiting()
    clock.now += timedelta(seconds=59)
    assert wait.waiting()
    clock.now += timedelta(seconds=1)
    assert not wait.waiting()
    assert not wait.failed()  # the count started again


def test_failures_spread_over_more_than_10_minutes_never_wait() -> None:
    clock = Clock()
    wait = SignInWait(clock)
    for _ in range(10):
        assert not wait.failed()
        clock.now += timedelta(minutes=3)
    assert not wait.waiting()


def test_a_good_sign_in_clears_the_count() -> None:
    wait = SignInWait(Clock())
    for _ in range(4):
        wait.failed()
    wait.succeeded()
    assert not wait.failed()
