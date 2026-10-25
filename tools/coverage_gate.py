"""Per-file coverage floors (Rulebook §5.2).

A global average can hide an untested file, so every file is checked on its own.
Reads ``coverage.json`` (written by ``pytest --cov-report=json``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# (path fragment, minimum line %, minimum branch %). First match wins.
FLOORS: tuple[tuple[str, float, float], ...] = (
    ("agentzaaheen/core/policy/", 95.0, 95.0),
    ("agentzaaheen/core/vault/", 95.0, 95.0),
    ("agentzaaheen/core/runner/", 95.0, 95.0),
    ("agentzaaheen/core/llm_gateway/", 95.0, 95.0),
    ("agentzaaheen/core/metrics/", 95.0, 95.0),
    ("agentzaaheen/core/sdk/", 95.0, 95.0),
    ("agentzaaheen/plugins/connectors/", 80.0, 70.0),
    ("", 90.0, 85.0),
)


def floor_for(path: str) -> tuple[float, float]:
    """Return the (line %, branch %) floor that applies to ``path``."""
    normalised = path.replace("\\", "/")
    for fragment, lines, branches in FLOORS:
        if fragment in normalised:
            return lines, branches
    raise AssertionError("FLOORS must end with a catch-all entry")  # pragma: no cover


def check(report: Mapping[str, Any]) -> list[str]:
    """Return one message per file below its floor."""
    failures: list[str] = []
    for path, data in sorted(report.get("files", {}).items()):
        summary = data["summary"]
        statements = summary["num_statements"]
        branches = summary.get("num_branches", 0)
        if statements == 0:
            continue
        line_pct = 100.0 * summary["covered_lines"] / statements
        branch_pct = 100.0 * summary.get("covered_branches", 0) / branches if branches else 100.0
        min_lines, min_branches = floor_for(path)
        if line_pct < min_lines or branch_pct < min_branches:
            failures.append(
                f"{path}: lines {line_pct:.1f}% (min {min_lines}%), "
                f"branches {branch_pct:.1f}% (min {min_branches}%)"
            )
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    """Check ``coverage.json`` (or the given file); return 1 if any file is below its floor."""
    args = list(sys.argv[1:] if argv is None else argv)
    report_path = Path(args[0] if args else "coverage.json")
    if not report_path.exists():
        print(f"coverage-gate: {report_path} not found. Run pytest first (could not run, not a pass).")
        return 1
    failures = check(json.loads(report_path.read_text(encoding="utf-8")))
    if failures:
        print("coverage-gate: files below their coverage floor:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("coverage-gate: every file meets its floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
