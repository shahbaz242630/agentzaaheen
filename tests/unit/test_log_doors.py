"""Everything in or through Agent Zaaheen is logged (ADR-034, slice 2): one log point at each shared door.

The doors are the few places everything passes: the API, every outside call, every
database write, chat tool runs, the Customize page's decisions, alerts, scheduled jobs
and settings read at start. This test lists them and fails if one loses its log line,
and it fails if a database write is made without going through ``Database.write``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "agentzaaheen"
STORE = SRC / "core" / "store"
_WRITES = {"INSERT", "UPDATE", "DELETE"}

# (door, file, function as Class.method or name, the events it must be able to say)
DOORS: list[tuple[str, Path, str, set[str]]] = [
    (
        "the API: every request",
        SRC / "core/api/app.py",
        "_said",
        {"api_answered", "api_failed", "workspace_api_answered", "workspace_api_failed"},
    ),
    (
        "outside calls: connectors",
        SRC / "core/net/client.py",
        "JsonClient._get",
        {"net_answered", "net_failed"},
    ),
    ("outside calls: retries", SRC / "core/net/client.py", "JsonClient._back_off", {"net_retry"}),
    ("outside calls: downloads", SRC / "core/net/client.py", "JsonClient._read_link", {"net_download"}),
    (
        "outside calls: uptime",
        SRC / "core/net/probe.py",
        "StatusProbe.status_of",
        {"probe_answered", "probe_failed"},
    ),
    (
        "outside calls: the model",
        SRC / "core/net/model.py",
        "ModelClient.chat",
        {"model_answered", "model_call_failed"},
    ),
    (
        "outside calls: the phone",
        SRC / "core/net/push.py",
        "PushClient._post",
        {"push_sent", "push_failed", "push_secret_refused"},
    ),
    (
        "before anything leaves: findings",
        SRC / "core/runner/run.py",
        "Runner._said_leaks",
        {"agent_findings_leak_checked"},
    ),
    # The log store: only failures, because a line for each send would itself be sent (a loop).
    ("outside calls: the log store", SRC / "core/net/otlp.py", "OtlpSender._post", {"telemetry_dropped"}),
    # ``Database.write`` and ``Scoped.write`` (inside the walls, ADR-039) both log through ``_logged``.
    (
        "database writes",
        SRC / "core/store/db.py",
        "_logged",
        {"store_saved", "store_unchanged", "store_write_failed"},
    ),
    (
        "chat tools",
        SRC / "core/llm_gateway/chat.py",
        "FactChat._run",
        {"chat_tool_used", "chat_tool_failed", "chat_tool_refused"},
    ),
    (
        "Customize decisions",
        SRC / "core/policy/registry_control.py",
        "RegistryControl._audit",
        {"registry_decided"},
    ),
    (
        "passkey steps",
        SRC / "core/policy/passkey_approvals.py",
        "PasskeyApprovals._audit",
        {"passkey_decided"},
    ),
    ("alerts", SRC / "core/notify/__init__.py", "deliver", {"alert_sent", "alert_not_sent"}),
    (
        "scheduled: uptime round",
        ROOT / "tools/dashboard_uptime.py",
        "UptimeLive.round",
        {"uptime_round_done"},
    ),
    (
        "scheduled: uptime crash",
        ROOT / "tools/dashboard_uptime.py",
        "UptimeLive._run",
        {"uptime_round_failed"},
    ),
    (
        "scheduled: hourly re-read",
        ROOT / "tools/dashboard_reread.py",
        "HourlyReads.round",
        {"reread_done", "reread_failed", "reread_crashed"},
    ),
    (
        "scheduled: revenue sync",
        SRC / "core/scheduler/collect.py",
        "_sync",
        {"revenue_synced", "revenue_sync_failed", "revenue_sync_crashed"},
    ),
    (
        "settings: uptime list",
        ROOT / "tools/dashboard_uptime.py",
        "uptime_list",
        {"settings_loaded", "settings_refused"},
    ),
    (
        "settings: Supabase list",
        ROOT / "tools/dashboard_supabase.py",
        "supabase_list",
        {"settings_loaded", "settings_refused"},
    ),
    (
        "settings: Healthchecks list",
        ROOT / "tools/dashboard_healthchecks.py",
        "healthchecks_list",
        {"settings_loaded", "settings_refused"},
    ),
    (
        "scheduled jobs: Monitoring reads Healthchecks",
        ROOT / "tools/dashboard_healthchecks.py",
        "HealthchecksReader.refresh",
        {"healthchecks_read_done", "healthchecks_read_failed"},
    ),
    (
        "outside calls: certificate reads",
        SRC / "core/net/cert.py",
        "CertProbe.certificate_of",
        {"cert_answered", "cert_failed"},
    ),
    (
        "settings: domain list",
        ROOT / "tools/dashboard_expiry.py",
        "domains_list",
        {"settings_loaded", "settings_refused"},
    ),
    (
        "scheduled jobs: Monitoring reads certificate and domain dates",
        ROOT / "tools/dashboard_expiry.py",
        "ExpiryReader.refresh",
        {"expiry_read_done", "expiry_read_failed"},
    ),
    (
        "settings: fix guides",
        ROOT / "tools/fix_guides.py",
        "fixes_list",
        {"settings_loaded", "settings_refused"},
    ),
    ("settings: the registry", SRC / "core/registry/load.py", "load_registry", {"registry_item_refused"}),
    (
        "settings: kill switches",
        SRC / "core/policy/ledger.py",
        "load_kill_switches",
        {"kill_switches_unreadable", "kill_switch_row_invalid"},
    ),
]


def _function(path: Path, qualname: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    body: list[ast.stmt] = ast.parse(path.read_text(encoding="utf-8")).body
    *owners, name = qualname.split(".")
    for owner in owners:
        body = next(n.body for n in body if isinstance(n, ast.ClassDef) and n.name == owner)
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise LookupError(qualname)


def _said(node: ast.AST) -> set[str]:
    return {
        call.args[1].value
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "say"
        and len(call.args) > 1
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    }


@pytest.mark.parametrize(("door", "path", "where", "events"), DOORS, ids=[d[0] for d in DOORS])
def test_each_door_keeps_its_log_point(door: str, path: Path, where: str, events: set[str]) -> None:
    missing = events - _said(_function(path, where))
    assert not missing, f"{door}: {path.relative_to(ROOT)}:{where} no longer says {sorted(missing)}"


def test_the_api_middleware_logs_every_api_request() -> None:
    middleware = _function(SRC / "core/api/app.py", "_with_headers")
    called = {
        c.func.id for c in ast.walk(middleware) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "_said" in called


def _write_sql(tree: ast.Module) -> set[str]:
    """Module constants holding an INSERT, UPDATE or DELETE statement."""
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            words = node.value.value.split(None, 1)
            if words and words[0].upper() in _WRITES:
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _store_writers() -> list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    out = []
    for path in sorted(STORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sql = _write_sql(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and sql & {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            }:
                out.append((f"{path.name}:{node.name}", node))
    return out


def test_the_store_has_writes_to_check() -> None:
    assert len(_store_writers()) >= 19  # the check below would pass vacuously if this found nothing


def test_every_database_write_goes_through_the_logged_door() -> None:
    """A write borrows its connection only with ``Database.write``, or is given one by its caller."""
    wrong = []
    for where, node in _store_writers():
        borrowed = {
            n.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and n.attr in {"connection", "transaction"}
        }
        if borrowed:
            wrong.append(f"{where} uses .{'/.'.join(sorted(borrowed))}() instead of .write(...)")
    assert wrong == []
