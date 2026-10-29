"""Passkey checks (ADR-SEC-007), with a software authenticator making real signatures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.auth import (
    CHALLENGE_TTL,
    SIGN_IN,
    Challenges,
    KnownPasskey,
    NewPasskey,
    PasskeyError,
    Site,
    approve_options,
    enrol_options,
    verify_approval,
    verify_enrol,
)
from tests.fixtures.soft_authenticator import SoftAuthenticator

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SITE = Site(origin="http://localhost:8765")
PURPOSE = "approve:6f1c7a52-3b9e-4d0a-9a43-1f6f1d2b8c11"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def enrolled(device: SoftAuthenticator) -> KnownPasskey:
    challenge = b"c" * 32
    made: NewPasskey = verify_enrol(SITE, challenge, device.register(enrol_options(SITE, challenge, [])))
    return KnownPasskey(made.credential_id, made.public_key, made.sign_count)


def test_set_up_asks_this_laptop_for_a_verified_passkey() -> None:
    options = enrol_options(SITE, b"c" * 32, [b"x" * 16])
    assert options["rp"] == {"name": "Agent Zaaheen", "id": "localhost"}
    selection = options["authenticatorSelection"]
    assert selection == {"authenticatorAttachment": "platform", "residentKey": "preferred",
                         "requireResidentKey": False, "userVerification": "required"}  # fmt: skip
    assert len(options["excludeCredentials"]) == 1  # type: ignore[arg-type]


def test_a_new_passkey_keeps_only_public_data() -> None:
    device = SoftAuthenticator()
    known = enrolled(device)
    assert known.credential_id == device.credential_id
    assert known.sign_count == 0
    assert len(known.public_key) > 32


@pytest.mark.parametrize(
    "answer",
    [
        lambda d, o: d.register(o, origin="http://evil.example"),
        lambda d, o: d.register(o, verified=False),  # a touch without PIN or fingerprint is not enough
        lambda d, o: d.register({**o, "challenge": "b3RoZXI"}),
    ],
)
def test_set_up_refuses_another_origin_no_verification_or_another_challenge(answer: object) -> None:
    device = SoftAuthenticator()
    options = enrol_options(SITE, b"c" * 32, [])
    with pytest.raises(PasskeyError) as caught:
        verify_enrol(SITE, b"c" * 32, answer(device, options))  # type: ignore[operator]
    assert caught.value.code == "passkey.not_verified"


def test_an_approval_signed_by_the_stored_passkey_holds_and_moves_the_counter() -> None:
    device = SoftAuthenticator()
    known = enrolled(device)
    options = approve_options(SITE, b"a" * 32, [known])
    assert options["userVerification"] == "required"
    used, count = verify_approval(SITE, b"a" * 32, device.sign(options), [known])
    assert (used, count) == (known, 1)


@pytest.mark.parametrize(
    ("knob", "value"),
    [
        ("origin", "http://127.0.0.1:8765"),
        ("rp_id", "example.com"),
        ("verified", False),
        ("challenge", "b3RoZXI"),  # signed for another challenge (another request)
        ("tamper", True),
    ],
)
def test_an_approval_is_refused_when_anything_is_off(knob: str, value: object) -> None:
    device = SoftAuthenticator()
    known = enrolled(device)
    options = approve_options(SITE, b"a" * 32, [known])
    with pytest.raises(PasskeyError) as caught:
        verify_approval(SITE, b"a" * 32, device.sign(options, **{knob: value}), [known])  # type: ignore[arg-type]
    assert caught.value.code == "passkey.not_verified"


def test_a_counter_going_backwards_is_refused() -> None:
    device = SoftAuthenticator()
    known = enrolled(device)
    ahead = KnownPasskey(known.credential_id, known.public_key, 50)
    with pytest.raises(PasskeyError):
        verify_approval(SITE, b"a" * 32, device.sign(approve_options(SITE, b"a" * 32, [ahead])), [ahead])


def test_a_passkey_we_do_not_know_is_refused() -> None:
    known = enrolled(SoftAuthenticator())
    stranger = SoftAuthenticator()
    with pytest.raises(PasskeyError) as caught:
        verify_approval(SITE, b"a" * 32, stranger.sign({"challenge": "YQ"}), [known])
    assert caught.value.code == "passkey.unknown"


def test_an_oversized_answer_is_refused_before_any_check() -> None:
    with pytest.raises(PasskeyError) as caught:
        verify_enrol(SITE, b"c" * 32, {"padding": "x" * 20_000})
    assert caught.value.code == "passkey.answer_too_big"


def test_a_challenge_is_used_once_for_one_purpose_and_lapses() -> None:
    clock = Clock()
    challenges = Challenges(clock)
    first = challenges.issue(PURPOSE)
    assert len(first) == 32
    assert challenges.take(PURPOSE) == first
    with pytest.raises(PasskeyError) as again:
        challenges.take(PURPOSE)
    assert again.value.code == "passkey.no_challenge"
    with pytest.raises(PasskeyError):
        challenges.take("enrol")  # never issued
    challenges.issue(PURPOSE)
    clock.now = NOW + CHALLENGE_TTL + timedelta(seconds=1)
    with pytest.raises(PasskeyError) as late:
        challenges.take(PURPOSE)
    assert late.value.code == "passkey.challenge_expired"


def test_a_new_challenge_replaces_the_old_one_and_odd_purposes_are_refused() -> None:
    challenges = Challenges(Clock())
    old = challenges.issue("enrol")
    new = challenges.issue("enrol")
    assert old != new
    assert challenges.take("enrol") == new
    for odd in ("approve:anything", "approve:", "delete", ""):
        with pytest.raises(ValueError, match="purpose"):
            challenges.issue(odd)


def test_a_sign_in_challenge_is_its_own_purpose() -> None:
    """ADR-SEC-010: a sign-in signature fits no approval, and an approval's fits no sign-in."""
    challenges = Challenges(Clock())
    sign_in = challenges.issue(SIGN_IN)
    approval = challenges.issue(PURPOSE)
    assert sign_in != approval
    assert challenges.take(SIGN_IN) == sign_in
    with pytest.raises(PasskeyError):
        challenges.take(SIGN_IN)
    for odd in ("signin:x", "sign-in", "SIGNIN"):
        with pytest.raises(ValueError, match="purpose"):
            challenges.issue(odd)


def test_open_challenges_are_capped() -> None:
    challenges = Challenges(Clock())
    ids = [f"approve:00000000-0000-0000-0000-{n:012d}" for n in range(40)]
    for purpose in ids:
        challenges.issue(purpose)
    with pytest.raises(PasskeyError):
        challenges.take(ids[0])  # the oldest were dropped
    assert len(challenges.take(ids[-1])) == 32


def test_the_site_origin_must_be_a_plain_web_address() -> None:
    with pytest.raises(ValueError, match="should match pattern"):
        Site(origin="javascript:alert(1)")
    assert Site(origin="http://localhost:9000").rp_id == "localhost"
