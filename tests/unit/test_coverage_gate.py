from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import coverage_gate


def _report(
    path: str, lines: int, covered: int, branches: int = 0, covered_branches: int = 0
) -> dict[str, object]:
    summary = {
        "num_statements": lines,
        "covered_lines": covered,
        "num_branches": branches,
        "covered_branches": covered_branches,
    }
    return {"files": {path: {"summary": summary}}}


def test_floors_are_stricter_for_authority_code() -> None:
    assert coverage_gate.floor_for("src/yaqzan/core/policy/gate.py") == (95.0, 95.0)
    assert coverage_gate.floor_for(r"src\yaqzan\core\vault\box.py") == (95.0, 95.0)
    assert coverage_gate.floor_for("src/yaqzan/plugins/connectors/paddle/client.py") == (80.0, 70.0)
    assert coverage_gate.floor_for("tools/path_guard.py") == (90.0, 85.0)


def test_file_meeting_floor_passes() -> None:
    assert coverage_gate.check(_report("tools/x.py", 10, 9, 4, 4)) == []


def test_low_lines_fail() -> None:
    assert len(coverage_gate.check(_report("tools/x.py", 10, 8))) == 1


def test_low_branches_fail() -> None:
    assert len(coverage_gate.check(_report("tools/x.py", 10, 10, 10, 5))) == 1


def test_empty_files_are_skipped() -> None:
    assert coverage_gate.check(_report("src/yaqzan/core/api/__init__.py", 0, 0)) == []


def test_main_reads_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_report("tools/x.py", 10, 10)), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_report("src/yaqzan/core/policy/g.py", 10, 5)), encoding="utf-8")
    assert coverage_gate.main([str(good)]) == 0
    assert coverage_gate.main([str(bad)]) == 1
    assert "policy" in capsys.readouterr().out


def test_missing_report_is_a_failure_not_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert coverage_gate.main([str(tmp_path / "nope.json")]) == 1
    assert "could not run" in capsys.readouterr().out
