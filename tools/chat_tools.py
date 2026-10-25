"""The dashboard chat on this laptop: its model and its read-only tools (F05, F06, ADR-029).

:func:`model_chat` connects the chat to a model server. It gets two tools,
each with a closed list of arguments:

- ``load_skill``: the instructions of a skill the model may use on its own
  (``invocation: auto``), refused if the skill's files changed since start.
- ``failed_build_log``: the newest failed build's log on one project's main
  branch, read from GitHub only when asked (needs the read-only token), and
  always handed to the model as untrusted text.
- ``fix_guide`` (with ``--fixes``): what serves one part of a project and what
  to check or fix when it is down (``tools.fix_guides``, our own local file).
- ``read_logs`` (with ``--log-dir``, while the ``read-logs`` skill is on): one
  project's recent warnings and errors from the kept logs (``tools.log_tool``).

The skill catalogue in the prompt lists the same skills, one line each.

With registry switches (ADR-030), :class:`SwitchedChat` asks the database
before every question which items are on, and offers only those: an off skill
leaves the catalogue and ``load_skill``; an off GitHub connector takes
``failed_build_log`` away. If the switches cannot be read, the chat gets no
tools at all (fail closed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from tools.fix_guides import Project, fix_guide_tool
from tools.log_tool import READ_LOGS_SKILL, read_logs_tool
from yaqzan.core.api import ChatBackend
from yaqzan.core.llm_gateway import ChatModel, ChatReply, FactChat, Inspect, Tool, catalogue
from yaqzan.core.net import DownloadPolicy, JsonClient, ModelClient, NetError, NetPolicy
from yaqzan.core.policy import Standing
from yaqzan.core.registry import Invocation, ItemError, Kind, Registry, load_registry, skill_instructions
from yaqzan.core.sdk import FailedStepLog, get_logger, say
from yaqzan.core.store import StoreError
from yaqzan.core.telemetry import LogFolder
from yaqzan.plugins.connectors import github

Clock = Callable[[], datetime]
# The model menu entry the local model trial connects (see SamplePortfolio.models).
LOCAL_MODEL_ID = "local-gemma"
# The registry id of the connector that ``failed_build_log`` reads through (its plugin.yaml).
GITHUB_CONNECTOR = "github"
CHANGED_SKILL = (
    "This skill was not opened: its files changed (or went missing) since Agent Zaaheen started, "
    "so it has not been reviewed in this form. Answer without it and say so."
)


type ToolSet = tuple[list[Tool], str]


@dataclass(frozen=True)
class Switched:
    """How to build the chat's tools for what is on (``None``: switches unreadable), and how to ask."""

    build: Callable[[Standing], ToolSet]
    standing: Callable[[], Awaitable[Standing]]


class SwitchedChat:
    """The chat, given fresh tools for every question: only what is switched on now."""

    def __init__(self, model: ChatModel, switched: Switched, inspect: Inspect | None = None) -> None:
        self.model_id = LOCAL_MODEL_ID
        self._model = model
        self._switched = switched
        self._inspect = inspect
        self._log = get_logger("yaqzan.chat", model=LOCAL_MODEL_ID)

    async def ask(self, question: str, facts: str, workspace: str | None = None) -> ChatReply:
        """Answer with the tools of the items that are on; with none if the switches can't be read."""
        try:
            tools, skills = self._switched.build(await self._switched.standing())
        except StoreError as exc:
            say(self._log, "chat_switches_unreadable", code=exc.code.value, effect="no_tools")
            tools, skills = [], ""
        chat = FactChat(self._model, self.model_id, tools=tools, skills=skills, inspect=self._inspect)
        return await chat.ask(question, facts, workspace)


def model_chat(  # noqa: PLR0913  # the server, then each optional part of the chat
    model_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    tools: Sequence[Tool] = (),
    skills: str = "",
    *,
    switched: Switched | None = None,
    inspect: Inspect | None = None,
) -> ChatBackend | None:
    """The chat backed by the model server at ``model_url``, or ``None`` (after saying why) if refused.

    With ``switched`` its tools follow the registry switches; otherwise it keeps ``tools`` and ``skills``.
    ``inspect`` lets the Board's chat look inside a workspace, each look recorded first (ADR-039);
    without it the Board's chat reads summaries only.
    """
    try:
        client = ModelClient(model_url, transport=transport)
    except NetError as exc:
        print(f"dashboard: the model address was refused ({exc.net_code.value}).")
        print("  Use the local model server, e.g. http://127.0.0.1:8766")
        return None
    if switched is not None:
        return SwitchedChat(client, switched, inspect)
    return FactChat(client, LOCAL_MODEL_ID, tools=tools, skills=skills)


def skill_tool(
    registry: Registry, plugins_root: Path, on: frozenset[str] | None = None
) -> tuple[Tool | None, str]:
    """The ``load_skill`` tool and the catalogue for the skills a model may pick itself (none: ``None``).

    ``on``, when given, leaves out every skill not switched on.
    """
    skills = [
        skill
        for skill in registry.skills
        if skill.invocation is Invocation.AUTO and (on is None or skill.id in on)
    ]
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
        description="Open a skill's instructions (Agent Zaaheen's own playbook).",
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
        workspace_of=lambda project: project,
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
) -> ToolSet:
    """Every chat tool available on this run, and the skill catalogue for the prompt (no switches)."""
    return tools_for(load_registry(plugins_root), plugins_root, token, repos, clock, transport=transport)


def tools_for(  # noqa: PLR0913  # the registry, where it lives, the outside world, and what is on
    registry: Registry,
    plugins_root: Path,
    token: SecretStr | None,
    repos: Mapping[str, str],
    clock: Clock,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    standing: Standing | None = None,
    fixes: Mapping[str, Project] | None = None,
    logs: LogFolder | None = None,
) -> ToolSet:
    """The chat's tools and skill catalogue for what is on (``standing``; ``None``: no switches, so all)."""
    skill, skills = skill_tool(registry, plugins_root, None if standing is None else standing.on(Kind.SKILL))
    tools = [skill] if skill is not None else []
    github_on = standing is None or GITHUB_CONNECTOR in standing.on(Kind.CONNECTOR)
    if token is not None and github_on:
        tools.append(build_log_tool(token, repos, clock, transport))
    guide = fix_guide_tool(fixes or {})
    if guide is not None:
        tools.append(guide)
    if standing is None or READ_LOGS_SKILL in standing.on(Kind.SKILL):
        reader = read_logs_tool(logs, repos)  # the same projects: each one's id
        if reader is not None:
            tools.append(reader)
    return tools, skills
