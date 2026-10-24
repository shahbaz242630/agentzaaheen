"""The workspace directory behind the door (ADR-039): the server's own list, by address."""

from __future__ import annotations

from uuid import uuid4

import pytest

from yaqzan.core.api.workspaces import WorkspaceDirectory
from yaqzan.core.store import WorkspaceRow


def row(slug: str) -> WorkspaceRow:
    return WorkspaceRow(workspace_id=uuid4(), slug=slug, name=slug.title())


def test_finds_by_address_and_keeps_the_order_given() -> None:
    eva, zaaheen = row("eva"), row("zaaheen")
    directory = WorkspaceDirectory([zaaheen, eva])
    assert directory.all() == (zaaheen, eva)
    assert directory.find("eva") == eva
    assert directory.find("sandoq-kin") is None


def test_two_workspaces_with_one_address_are_refused() -> None:
    with pytest.raises(ValueError, match="unique"):
        WorkspaceDirectory([row("eva"), row("eva")])
