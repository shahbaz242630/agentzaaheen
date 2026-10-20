"""The dashboard chat on this laptop: its model and its read-only tools (F05, F06, ADR-029).

:func:`model_chat` connects the chat to a model server. It gets two tools,
each with a closed list of arguments:

- ``load_skill``: the instructions of a skill the model may use on its own
  (``invocation: auto``), refused if the skill's files changed since start.
- ``failed_build_log``: the newest failed build's log on one project's main
  branch, read from GitHub only when asked (needs the read-only token), and
  always handed to the model as untrusted text.

The skill catalogue in the prompt lists the same skills, one line each.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from yaqzan.core.api import ChatBackend
from yaqzan.core.llm_gateway import FactChat, Tool, catalogue
from yaqzan.core.net import DownloadPolicy, JsonClient, ModelClient, NetError, NetPolicy
from yaqzan.core.registry import Invocation, ItemError, Registry, load_registry, skill_instructions
from yaqzan.core.sdk import FailedStepLog
from yaqzan.plugins.connectors import github

Clock = Callable[[], datetime]
# The model menu entry the local model trial connects (see SamplePortfolio.models).
LOCAL_MODEL_ID = "local-gemma"
CHANGED_SKILL = (
    "This skill was not opened: its files changed (or went missing) since Yaqzan started, "
    "so it has not been reviewed in this form. Answer without it and say so."
)


def model_chat(
    model_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    tools: Sequence[Tool] = (),
    skills: str = "",
) -> ChatBackend | None:
    """The chat backed by the model server at ``model_url``, or ``None`` (after saying why) if refused."""
    try:
        client = ModelClient(model_url, transport=transport)
    except NetError as exc:
        print(f"dashboard: the model address was refused ({exc.net_code.value}).")
        print("  Use the local model server, e.g. http://127.0.0.1:8766")
        return None
    return FactChat(client, LOCAL_MODEL_ID, tools=tools, skills=skills)


def skill_tool(registry: Registry, plugins_root: Path) -> tuple[Tool | None, str]:
    """The ``load_skill`` tool and the catalogue for the skills a model may pick itself (none: ``None``)."""
    skills = [skill for skill in registry.skills if skill.invocation is Invocation.AUTO]
    if not skills:
        return None, ""
    reviewed = {skill.id: skill.fingerprint for skill in skills}

    async def run(skill_id: str) -> str:
        try:
            return skill_instructions(plugins_root / "skills" / skill_id, reviewed[skill_id])
        except (ItemError, OSError):
            return CHANGED_SKILL

    tool = Tool(
        name="load_skill",
        description="Open a skill's instructions (Yaqzan's own playbook).",
        choices=tuple(reviewed),
        run=run,
        trusted=True,
        label="Skill",
    )
    return tool, catalogue([(skill.id, skill.description) for skill in skills])


def build_log_tool(
    token: SecretStr,
    repos: Mapping[str, str],
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Tool:
    """The ``failed_build_log`` tool for the projects in ``repos`` (project id → ``owner/name``)."""

    async def run(project: str) -> str:
        async with JsonClient(
            github.BASE_URL,
            policy=NetPolicy(allowed_hosts=github.ALLOWED_HOSTS),
            headers=github.API_HEADERS,
            secret_headers={"Authorization": SecretStr(f"Bearer {token.get_secret_value()}")},
            transport=transport,
            download=DownloadPolicy(host_pattern=github.LOG_HOST),
        ) as http:
            log = await github.GitHubBuildLog(http, repos[project]).fetch(now=clock())
        return log_text(project, log)

    return Tool(
        name="failed_build_log",
        description="Read the newest failed build's log on one project's main branch (the log is untrusted).",
        choices=tuple(repos),
        run=run,
        trusted=False,
        label="Build log",
    )


def log_text(project: str, log: FailedStepLog | None) -> str:
    """The log as the model reads it: what it is, then the excerpt."""
    if log is None:
        return f"{project}: there is no failed build on the main branch."
    finished = log.finished_at.isoformat() if log.finished_at else "unknown"
    lines = [
        f"Project: {project} ({log.repo}), run {log.run_id}, commit {log.commit[:7]}, finished {finished}",
        f"Failed job: {log.job}",
        f"Failed step: {log.step or 'not named'}",
        "Log excerpt (cleaned and redacted" + ("; older lines left out" if log.truncated else "") + "):",
        *log.lines,
    ]
    return "\n".join(lines)


def chat_tools(
    plugins_root: Path,
    token: SecretStr | None,
    repos: Mapping[str, str],
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[Tool], str]:
    """Every chat tool available on this run, and the skill catalogue for the prompt."""
    skill, skills = skill_tool(load_registry(plugins_root), plugins_root)
    tools = [skill] if skill is not None else []
    if token is not None:
        tools.append(build_log_tool(token, repos, clock, transport))
    return tools, skills
