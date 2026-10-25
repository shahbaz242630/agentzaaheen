"""The chat's ``read_logs`` tool: one project's recent warnings and errors, from the kept logs (ADR-034).

It reads the laptop's 30-day log files (``--log-dir``) through
``core.telemetry.LogFolder``, so it sees only what the log store may receive:
each line's event code, its plain-English what/why/next, and codes, names,
counts and durations. The model picks a project from a closed list; the tool
returns that project's newest :data:`LINES` warnings and errors of today and
yesterday, newest first. It is offered only while the ``read-logs`` skill is
switched on (the registry switches, ADR-030), and the text is handed to the
model as untrusted data, like everything read back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from yaqzan.core.llm_gateway import Tool
from yaqzan.core.telemetry import LogFolder, LogPage, LogQuery

# The registry id of the skill this tool belongs to (its SKILL.md folder).
READ_LOGS_SKILL = "read-logs"
LINES = 40
DAYS = 2


def log_lines_text(project: str, page: LogPage) -> str:
    """The lines as the model reads them: what was searched, then one line each, newest first."""
    head = f"Project: {project}. Warnings and errors of today and yesterday (UTC), newest first."
    if not page.lines:
        return f"{head}\nNone found."
    out = [head]
    for line in page.lines:
        when, level = line.get("timestamp") or "time unknown", str(line.get("level")).upper()
        parts = [f"{when} {level} {line.get('event')}:", str(line.get("what") or "(no sentence)")]
        if line.get("why"):
            parts.append(f"Why: {line.get('why')}")
        if line.get("next"):
            parts.append(f"Next: {line.get('next')}")
        out.append(" ".join(parts))
    if page.more:
        out.append(f"Only the newest {len(page.lines)} are shown; there are more.")
    if page.cut:
        out.append("One day had so many lines that only its newest part was searched.")
    return "\n".join(out)


def read_logs_tool(logs: LogFolder | None, projects: Iterable[str]) -> Tool | None:
    """The ``read_logs`` tool for ``projects`` (none without kept logs or projects: ``None``)."""
    choices = tuple(sorted(set(projects)))
    if logs is None or not choices:
        return None

    async def run(project: str) -> str:
        query = LogQuery(level="warning", days=DAYS, project=project, limit=LINES)
        try:
            page = await asyncio.to_thread(logs.read, query)
        except OSError:
            return f"Project: {project}. The kept logs could not be read just now."
        return log_lines_text(project, page)

    return Tool(
        name="read_logs",
        description="Read one project's warnings and errors of today and yesterday from our own logs.",
        choices=choices,
        run=run,
        trusted=False,
        label="Logs",
        workspace_of=lambda project: project,
    )
