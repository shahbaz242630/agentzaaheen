"""The runner: the one way an agent run starts, through fixed checkpoints in code (ADR-036, F11).

What it does:
    :class:`Runner` starts every agent run: kill switches, the job's door
    (switches and caps), the agent's frozen role pack and its tools are
    checked before anything runs; the start is written down first; the end,
    its findings and its real cost are saved together; a run that died
    halfway is closed later as interrupted. Every step is logged in plain
    English and audited.

Public interface:
    Everything in ``__all__`` below. An agent is any :class:`AgentWork`; it can
    only be started with a :class:`RunTicket`, which only the runner makes.

Must never:
    Hold credentials or reach the network (agents reach tools through the
    policy gate and models through the model gateway); start an agent any
    other way than :meth:`Runner.run`; save a finding into another workspace.
"""

from agentzaaheen.core.runner.agent import (
    MAX_FINDINGS,
    AgentResult,
    AgentWork,
    FindingDraft,
    RunnerOnlyError,
    RunTicket,
)
from agentzaaheen.core.runner.run import CLOSE_GRACE, RunCode, Runner, RunReport

__all__ = [
    "CLOSE_GRACE",
    "MAX_FINDINGS",
    "AgentResult",
    "AgentWork",
    "FindingDraft",
    "RunCode",
    "RunReport",
    "RunTicket",
    "Runner",
    "RunnerOnlyError",
]
