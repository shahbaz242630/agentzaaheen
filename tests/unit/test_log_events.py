"""Plain-English logs (ADR-034): every log line goes through the catalogue and says what happened and why."""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from typing import Any

import pytest

from yaqzan.core.sdk import LOG_EVENTS, LogEvent, explain, get_logger, say
from yaqzan.core.sdk.events import placeholders
from yaqzan.core.telemetry.logs import LOG_FIELDS

ROOT = Path(__file__).resolve().parents[2]
CODE = [p for d in ("src", "tools") for p in sorted((ROOT / d).rglob("*.py"))]
CATALOGUE = ROOT / "src" / "yaqzan" / "core" / "sdk" / "events.py"
LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "msg"}


def calls() -> list[tuple[Path, ast.Call]]:
    out: list[tuple[Path, ast.Call]] = []
    for path in CODE:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out += [(path, node) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return out


def say_calls() -> list[tuple[Path, ast.Call]]:
    return [(p, c) for p, c in calls() if isinstance(c.func, ast.Name) and c.func.id == "say"]


def where(path: Path, call: ast.Call) -> str:
    return f"{path.relative_to(ROOT)}:{call.lineno}"


# --- the code ------------------------------------------------------------------------------------


def test_no_log_line_skips_the_catalogue() -> None:
    direct = [
        where(p, c)
        for p, c in calls()
        if p != CATALOGUE
        and isinstance(c.func, ast.Attribute)
        and c.func.attr in LEVELS
        and c.args
        and isinstance(c.args[0], ast.Constant)
        and isinstance(c.args[0].value, str)
    ]
    assert direct == [], "log through say(log, event, ...) so the line says what and why"


def test_every_say_names_a_listed_event_by_its_literal_name() -> None:
    names = [c.args[1] for _, c in say_calls()]
    assert len(names) >= len(LOG_EVENTS)
    odd = [
        where(p, c)
        for p, c in say_calls()
        if not (isinstance(c.args[1], ast.Constant) and c.args[1].value in LOG_EVENTS)
    ]
    assert odd == []


def test_every_event_is_used() -> None:
    used = {c.args[1].value for _, c in say_calls() if isinstance(c.args[1], ast.Constant)}
    assert set(LOG_EVENTS) - used == set()


def test_each_call_gives_the_fields_its_sentences_name() -> None:
    missing = []
    for path, call in say_calls():
        event = LOG_EVENTS[call.args[1].value]  # type: ignore[attr-defined]  # literal checked above
        given = {k.arg for k in call.keywords}
        if None in given:  # a **splat may add more; its keys are checked by the tests of that module
            continue
        needed = placeholders(event.what) | placeholders(event.why) | placeholders(event.next)
        needed |= {f for f in (event.explains, event.vendor) if f is not None}
        if needed - given:
            missing.append(f"{where(path, call)} lacks {sorted(needed - given)}")
    assert missing == []


# --- the catalogue -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LOG_EVENTS))
def test_each_event_says_what_and_why_and_what_next_when_it_matters(name: str) -> None:
    event = LOG_EVENTS[name]
    assert event.what.endswith(".")
    assert event.what[0].isupper()
    assert event.why or event.explains, "a why sentence, or a reason code to explain"
    if event.level != "info":
        assert event.next or event.explains, "warnings and errors say what to do next"
    for sentence in (event.why, event.next):
        assert not sentence or (sentence.endswith(".") and sentence[0].isupper())


@pytest.mark.parametrize("name", sorted(LOG_EVENTS))
def test_sentences_name_only_fields_the_log_store_may_receive(name: str) -> None:
    event = LOG_EVENTS[name]
    named = placeholders(event.what) | placeholders(event.why) | placeholders(event.next)
    named |= {f for f in (event.explains, event.vendor) if f is not None}
    assert named <= LOG_FIELDS, "free text (paths, tool arguments, errors) must never reach a sentence"


# --- say -----------------------------------------------------------------------------------------


def logged(name: str, /, **fields: object) -> dict[str, Any]:
    sink = io.StringIO()
    say(get_logger("test", sink=sink), name, **fields)
    return json.loads(sink.getvalue())  # type: ignore[no-any-return]


def test_a_line_carries_the_code_the_sentences_and_the_fields() -> None:
    line = logged("revenue_synced", subscriptions=3, payments=2, refunds=0)
    assert line["event"] == "revenue_synced"
    assert line["level"] == "info"
    assert line["what"] == "Revenue was synced: 3 subscriptions, 2 payments, 0 refunds."
    assert line["why"] == "The scheduled sync ran."
    assert line["next"] == ""
    assert (line["subscriptions"], line["payments"], line["refunds"]) == (3, 2, 0)


def test_flags_read_as_words_and_missing_values_as_unknown() -> None:
    line = logged("alert_sent", project="eva", down=True)
    assert line["what"] == "The phone alert for eva was sent (down: yes)."
    assert logged("alert_sent", project="eva", down=False)["what"].endswith("(down: no).")
    assert logged("chat_failed", code="net.timeout", status=None)["what"] == (
        "A question asked in the dashboard chat could not be answered (http unknown)."
    )


def test_a_reason_code_fills_why_and_next_from_the_error_catalogue() -> None:
    line = logged("alert_not_sent", project="eva", down=True, code="net.timeout")
    said = explain("net.timeout")
    assert (line["level"], line["why"], line["next"]) == ("warning", said.why, said.fix)
    assert line["code"] == "net.timeout"  # a field may be called code: say's own arguments are positional


def test_the_vendor_names_the_source_in_the_explanation() -> None:
    line = logged(
        "revenue_sync_failed", stage="fetch", reason_code="source.auth", source="paddle", http_status=401
    )
    assert line["why"] == explain("source.auth", vendor="paddle").why
    assert line["next"] == explain("source.auth", vendor="paddle").fix
    assert line["what"] == "The revenue sync from paddle failed at the fetch step (http 401)."


def test_an_explained_event_without_its_code_still_says_something() -> None:
    line = logged("chat_failed", status=502)
    assert line["why"] == explain("unknown").why
    assert line["next"] == explain("unknown").fix


def test_its_own_sentences_win_over_the_catalogue() -> None:
    line = logged("chat_switches_unreadable", code="store.unavailable", effect="no_tools")
    assert line["why"].startswith(
        "The switches that say which tools are on could not be read (store.unavailable)"
    )
    assert line["next"] == "Check the laptop database is running (uv run python -m tools.laptop_db status)."


def test_errors_are_written_at_error_level() -> None:
    line = logged("kill_switches_unreadable", reason_code="store.unavailable", effect="stop_all")
    assert (line["level"], line["what"]) == ("error", "Every agent action was stopped.")


def test_an_unlisted_event_is_still_written_and_says_so() -> None:
    line = logged("brand_new_thing", count=1)
    assert line["level"] == "warning"
    assert line["what"] == "Something happened that has no description yet (brand_new_thing)."
    assert line["why"] == "The cause is not known."
    assert "count" not in line  # an unlisted event's fields are not trusted


def test_an_event_without_a_why_says_the_cause_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(LOG_EVENTS, "bare", LogEvent("info", what="Something happened."))
    line = logged("bare")
    assert (line["what"], line["why"], line["next"]) == ("Something happened.", "The cause is not known.", "")


def test_own_and_catalogue_sentences_mix_part_by_part(monkeypatch: pytest.MonkeyPatch) -> None:
    said = explain("net.timeout")
    own_why = LogEvent("warning", what="A.", why="Own why.", explains="code")
    own_next = LogEvent("warning", what="B.", next="Own next.", explains="code")
    monkeypatch.setitem(LOG_EVENTS, "own_why", own_why)
    monkeypatch.setitem(LOG_EVENTS, "own_next", own_next)
    first, second = logged("own_why", code="net.timeout"), logged("own_next", code="net.timeout")
    assert (first["why"], first["next"]) == ("Own why.", said.fix)
    assert (second["why"], second["next"]) == (said.why, "Own next.")
