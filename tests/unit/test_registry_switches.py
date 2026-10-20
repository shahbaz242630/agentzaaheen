"""Whether a registry item counts as on (ADR-030): pure rules over the stored switch and the files."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from yaqzan.core.registry import Kind, State, StoredSwitch, by_item, may_request, may_switch_off, state_of

FILES = "a" * 64
CHANGED = "b" * 64


def stored(
    enabled: bool, fingerprint: str = FILES, kind: Kind = Kind.SKILL
) -> dict[tuple[Kind, str], StoredSwitch]:
    return by_item([StoredSwitch(kind=kind, item_id="a-skill", enabled=enabled, fingerprint=fingerprint)])


def test_our_item_with_nothing_stored_is_on() -> None:
    assert state_of(Kind.SKILL, "a-skill", FILES, {}, outside=False) is State.ON


@pytest.mark.parametrize("enabled", [True, False])
def test_an_outside_bundle_is_view_only_whatever_is_stored(enabled: bool) -> None:
    rows = stored(enabled, kind=Kind.PLUGIN)
    assert state_of(Kind.PLUGIN, "a-skill", FILES, rows, outside=True) is State.VIEW_ONLY
    assert state_of(Kind.PLUGIN, "a-skill", FILES, {}, outside=True) is State.VIEW_ONLY


@pytest.mark.parametrize("fingerprint", [FILES, CHANGED])
def test_stored_off_is_off_whatever_the_files(fingerprint: str) -> None:
    assert state_of(Kind.SKILL, "a-skill", fingerprint, stored(False), outside=False) is State.OFF


def test_stored_on_counts_only_for_the_approved_files() -> None:
    assert state_of(Kind.SKILL, "a-skill", FILES, stored(True), outside=False) is State.ON
    assert state_of(Kind.SKILL, "a-skill", CHANGED, stored(True), outside=False) is State.CHANGED


def test_a_row_for_another_kind_or_item_does_not_count() -> None:
    assert state_of(Kind.CONNECTOR, "a-skill", FILES, stored(False), outside=False) is State.ON
    assert state_of(Kind.SKILL, "b-skill", FILES, stored(False), outside=False) is State.ON


@pytest.mark.parametrize("state", list(State))
def test_what_may_be_done_in_each_state(state: State) -> None:
    assert may_switch_off(state) == (state is State.ON)
    assert may_request(state) == (state in {State.OFF, State.CHANGED})


@pytest.mark.parametrize(("item_id", "fingerprint"), [("Bad Id", FILES), ("a-skill", "not-a-hash")])
def test_stored_rows_are_checked(item_id: str, fingerprint: str) -> None:
    with pytest.raises(ValidationError):
        StoredSwitch(kind=Kind.SKILL, item_id=item_id, enabled=True, fingerprint=fingerprint)
