"""The dashboard's customer-name key: used when it is there, said plainly when not (F11 slice 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentzaaheen.core.vault import NamePrinter
from tools import dashboard, name_prints


def test_with_the_key_findings_are_checked_for_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "names.key"
    assert name_prints.make_key(key)
    assert isinstance(dashboard._name_printer(key), NamePrinter)
    assert "also checked for our customers' names" in capsys.readouterr().out


def test_without_the_key_the_dashboard_says_names_are_not_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dashboard._name_printer(tmp_path / "names.key") is None
    assert "not for our customers' names" in capsys.readouterr().out


def test_a_broken_key_is_said_and_not_used(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "names.key"
    key.write_text("not a key", encoding="utf-8")
    assert dashboard._name_printer(key) is None
    assert "could not be used" in capsys.readouterr().out
