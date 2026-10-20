"""Whether each registry item counts as on, judged from its stored switch and its files (ADR-030).

Pure rules, no storage: the caller passes what the store holds.

- An outside bundle is **view only**: shown, never used, whatever is stored
  (until bundles can be signed and checked, ADR-027).
- Our own item with nothing stored is **on**: it was reviewed in a PR.
- Stored "off" is **off**.
- Stored "on" counts only for the files that were approved: if the
  fingerprint differs now, the item is **changed** and stays off until it is
  approved again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from yaqzan.core.registry.models import FINGERPRINT, ITEM_ID, Kind


class State(StrEnum):
    """How an item stands now."""

    ON = "on"
    OFF = "off"
    CHANGED = "changed"  # approved once, but its files changed since: off until approved again
    VIEW_ONLY = "view_only"  # an outside bundle: listed, never used


class StoredSwitch(BaseModel):
    """What the store holds for one item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Kind
    item_id: str = Field(pattern=ITEM_ID, max_length=64)
    enabled: bool
    fingerprint: str = Field(pattern=FINGERPRINT)


type SwitchKey = tuple[Kind, str]


def by_item(stored: Iterable[StoredSwitch]) -> dict[SwitchKey, StoredSwitch]:
    """The stored switches keyed by (kind, id)."""
    return {(s.kind, s.item_id): s for s in stored}


def state_of(
    kind: Kind,
    item_id: str,
    fingerprint: str,
    stored: Mapping[SwitchKey, StoredSwitch],
    *,
    outside: bool,
) -> State:
    """How one item stands, given its current files' ``fingerprint``."""
    if outside:
        return State.VIEW_ONLY
    row = stored.get((kind, item_id))
    if row is None:
        return State.ON
    if not row.enabled:
        return State.OFF
    return State.ON if row.fingerprint == fingerprint else State.CHANGED


def may_switch_off(state: State) -> bool:
    """Only something that is on can be switched off (anything else is already off)."""
    return state is State.ON


def may_request(state: State) -> bool:
    """Asking to switch on makes sense for our own items that are off, for whatever reason."""
    return state in {State.OFF, State.CHANGED}
