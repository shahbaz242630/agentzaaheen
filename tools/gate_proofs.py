"""Gate proofs: prove every quality and security check can still FAIL.

"A green check is not proof a check ran." Each proof feeds a check something
deliberately broken and passes only if the check rejects it. If a tool is
misconfigured into accepting everything, this script turns CI red.

Usage:
    python -m tools.gate_proofs                     # local run; gitleaks optional
    python -m tools.gate_proofs --require-gitleaks  # CI: gitleaks must be present
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from tools import coverage_gate, path_guard

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "gate_proofs" / "fixtures"
PROOF_MODULE = ROOT / "src" / "yaqzan" / "plugins" / "connectors" / "_gate_proof_violation.py"


def _fails(argv: Sequence[str]) -> bool:
    """Return True if the command exits non-zero (i.e. the gate caught the problem)."""
    result = subprocess.run(list(argv), cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    return result.returncode != 0


def proof_ruff() -> bool:
    """Ruff must reject a file with unused imports, bare except, eval and print."""
    return _fails(["ruff", "check", "--no-cache", "--isolated", "--select", "F,E722,S307,T20", str(FIXTURES)])


def proof_mypy() -> bool:
    """Mypy --strict must reject untyped functions and wrong argument types."""
    return _fails(["mypy", "--strict", "--no-incremental", str(FIXTURES / "bad_types.py")])


def proof_import_boundaries() -> bool:
    """import-linter must reject a plugin that imports a core internal."""
    PROOF_MODULE.write_text('"""Gate proof: temporary."""\n\nimport yaqzan.core.store\n', encoding="utf-8")
    try:
        return _fails(["lint-imports", "--no-cache"])
    finally:
        PROOF_MODULE.unlink(missing_ok=True)


def proof_path_guard() -> bool:
    """The path guard must reject docs, handoffs and secrets."""
    bad = ["docs/02-PRD.md", "HANDOFF.md", ".env", "src/yaqzan/core/README.md"]
    return path_guard.check(bad) == bad


def proof_coverage_gate() -> bool:
    """The coverage gate must reject a policy file with 50% coverage."""
    report = {
        "files": {
            "src/yaqzan/core/policy/gate.py": {
                "summary": {
                    "num_statements": 10,
                    "covered_lines": 5,
                    "num_branches": 4,
                    "covered_branches": 2,
                }
            }
        }
    }
    return len(coverage_gate.check(json.loads(json.dumps(report)))) == 1


def proof_gitleaks() -> bool | None:
    """Gitleaks must find a freshly generated fake key. None = tool not installed."""
    binary = shutil.which("gitleaks")
    if binary is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        # Built at runtime so no secret-shaped string ever sits in the repository.
        fake = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
        (Path(tmp) / "leak.txt").write_text(f"token = {fake}\n", encoding="utf-8")
        return _fails([binary, "dir", tmp, "--no-banner", "--redact", "--exit-code", "1"])


def main(argv: Sequence[str] | None = None) -> int:
    """Run every proof; return 1 if any gate failed to catch its broken input."""
    parser = argparse.ArgumentParser(description="Prove each gate still fails on bad input.")
    parser.add_argument("--require-gitleaks", action="store_true")
    ns = parser.parse_args(argv)

    proofs: dict[str, Callable[[], bool | None]] = {
        "ruff": proof_ruff,
        "mypy": proof_mypy,
        "import-boundaries": proof_import_boundaries,
        "path-guard": proof_path_guard,
        "coverage-gate": proof_coverage_gate,
        "gitleaks": proof_gitleaks,
    }
    ok = True
    for name, proof in proofs.items():
        caught = proof()
        if caught is None:
            status = "COULD NOT RUN (tool missing)"
            if ns.require_gitleaks:
                ok = False
        elif caught:
            status = "ok, the gate caught the broken input"
        else:
            status = "FAILED, the gate accepted broken input"
            ok = False
        print(f"gate-proof {name:<18} {status}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
