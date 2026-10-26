"""The key list and the briefing's key lines (F10 key expiry, 26 Oct): dates by code, no secrets, no links."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agentzaaheen.core.api.summary import briefing
from tools.dashboard_sources import ListNotUsableError
from tools.key_dates import MAX_KEYS, KeyDate, KeyListError, key_dates_list, key_lines, load_key_dates

TODAY = date(2026, 12, 19)


def key(name: str, expires: date | str, where: str = "laptop") -> KeyDate:
    return KeyDate.model_validate(
        {"name": name, "where": where, "expires": expires, "renew": "make a new one"}
    )


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "keys.yaml"
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """
- {name: Claude key, where: laptop vault, expires: 2027-01-02, renew: "Claude Console > API keys"}
- {name: Server model key, where: laptop vault + server, expires: never, renew: "-"}
"""


def test_the_list_is_read_with_dates_and_never(tmp_path: Path) -> None:
    keys = load_key_dates(write(tmp_path, GOOD))
    assert [k.name for k in keys] == ["Claude key", "Server model key"]
    assert keys[0].expires == date(2027, 1, 2)
    assert keys[1].expires == "never"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("{a: 1}", "must be a list of keys"),
        ("[]", "must be a list of keys"),
        ("- [unclosed", "not valid YAML"),
        ("- {name: A, where: b, expires: 2027-13-40, renew: c}", "a date that does not exist"),
        ("- {name: A, where: b, expires: soon, renew: c}", "key 1: expires is not usable"),
        ("- {name: A, where: b, expires: 2027-01-02}", "key 1: renew is not usable"),
        ("- {name: A, where: b, expires: 2027-01-02, renew: c, secret: x}", "key 1: secret is not usable"),
        ("- {name: A, where: b, expires: 2027-01-02, renew: 'see https://x.example'}", "no web addresses"),
        ("- {name: A, where: b, expires: 2027-01-02, renew: 'go to www.example.com'}", "no web addresses"),
        ('- {name: "A\\nB", where: b, expires: 2027-01-02, renew: c}', "one line of plain text"),
        ("- {name: '', where: b, expires: 2027-01-02, renew: c}", "key 1: name is not usable"),
    ],
)
def test_a_list_that_is_not_usable_is_refused_with_the_reason(tmp_path: Path, text: str, why: str) -> None:
    with pytest.raises(KeyListError, match=why):
        load_key_dates(write(tmp_path, text))


def test_too_many_keys_or_too_big_or_missing_is_refused(tmp_path: Path) -> None:
    many = "".join(f"- {{name: K{i}, where: b, expires: never, renew: c}}\n" for i in range(MAX_KEYS + 1))
    with pytest.raises(KeyListError, match="more than"):
        load_key_dates(write(tmp_path, many))
    with pytest.raises(KeyListError, match="too big"):
        load_key_dates(write(tmp_path, "#" * 20_000))
    with pytest.raises(KeyListError, match="could not be read"):
        load_key_dates(tmp_path / "missing.yaml")


def test_without_a_list_there_are_no_keys_and_a_broken_one_stops_the_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert key_dates_list(None) == ()
    assert len(key_dates_list(write(tmp_path, GOOD))) == 2
    with pytest.raises(ListNotUsableError):
        key_dates_list(write(tmp_path, "[]"))
    assert "the key list must be a list of keys" in capsys.readouterr().out


def test_only_keys_ending_within_14_days_or_ended_are_said_soonest_first() -> None:
    keys = [
        key("Paddle", date(2027, 1, 16)),  # 28 days: not yet
        key("Claude key", date(2027, 1, 2), "laptop vault"),  # 14 days: said
        key("GitHub", date(2026, 12, 20)),  # tomorrow
        key("Old token", date(2026, 12, 1)),  # ended
        key("Server model key", "never"),
    ]
    assert key_lines(keys, TODAY) == (
        "Key ENDED 1 Dec 2026: Old token (laptop). Renew: make a new one",
        "Key ends in 1 day (20 Dec 2026): GitHub (laptop). Renew: make a new one",
        "Key ends in 14 days (2 Jan 2027): Claude key (laptop vault). Renew: make a new one",
    )


def test_a_key_ending_today_and_15_days_out() -> None:
    assert key_lines([key("A", TODAY)], TODAY) == ("Key ends TODAY: A (laptop). Renew: make a new one",)
    assert key_lines([key("A", date(2027, 1, 3))], TODAY) == ()


def test_many_keys_ending_are_capped_and_counted() -> None:
    five = [key(f"K{i}", date(2026, 12, 20 + i)) for i in range(5)]
    lines = key_lines(five, TODAY)
    assert len(lines) == 5
    assert lines[-1] == "... and 1 more key ends soon; see the key list."
    six = [*five, key("K9", date(2026, 12, 30))]
    assert key_lines(six, TODAY)[-1] == "... and 2 more keys end soon; see the key list."


def test_the_briefing_puts_key_lines_after_the_count_and_before_claude() -> None:
    _, lines = briefing(
        [],
        day=TODAY,
        local_time="07:00",
        late=False,
        keys=("Key ends in 1 day (20 Dec 2026): GitHub (laptop). Renew: x",),
        claude=("1 Claude run did not run.",),
    )
    assert lines[:3] == (
        "No project has a summary today.",
        "Key ends in 1 day (20 Dec 2026): GitHub (laptop). Renew: x",
        "1 Claude run did not run.",
    )
