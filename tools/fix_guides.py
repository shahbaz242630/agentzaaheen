"""Fix guides: for each project's part, what serves it and what to check when it is down (ADR-032, R4).

When the uptime checks say a part is down (``api``, ``sign-in``), the harness
must know **why** and **how to fix it**. The guides live in ``projects/fixes.yaml``
(git-ignored and path-guarded like the other project lists: they name hosting,
workers and dashboards). Each part says what serves it, what it depends on,
where its logs are, how it is deployed and rolled back, and the steps to take
for each reason the checks give::

    zaaheen:
      name: Zaaheen
      parts:
        api:
          serves: Cloudflare Worker zaaheen-account, custom domain api.zaaheen.com
          depends_on: [Clerk sign-in, Paddle live API]
          logs: wrangler tail zaaheen-account (from workers/account)
          deploy: by hand, npx wrangler deploy from workers/account
          rollback: Workers dashboard, zaaheen-account, Deployments, roll back
          owner: chairman
          fixes:
            server_error:
              - Read the Worker's logs for config_incomplete ...

Reasons are the checks' own codes: ``no_answer``, ``server_error``,
``not_found``, ``refused``, ``wrong_answer``. A reason a part does not cover falls back to
:data:`GENERAL` (steps true of any address). Steps are written for a human or a
read-only agent: checks to make, and fixes for the owner to approve.

Usage:
    uv run python -m tools.fix_guides projects/fixes.yaml                     # check the file
    uv run python -m tools.fix_guides projects/fixes.yaml zaaheen api          # one part's guide
    uv run python -m tools.fix_guides projects/fixes.yaml zaaheen api no_answer
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from tools.dashboard_sources import ListNotUsableError
from yaqzan.core.api import SamplePortfolio
from yaqzan.core.llm_gateway import Tool
from yaqzan.core.sdk import get_logger, say
from yaqzan.core.sdk.uptime import MAX_PART, PART

MAX_FILE_BYTES = 128 * 1024
MAX_TEXT = 300
MAX_STEPS = 12

type Reason = Literal["no_answer", "server_error", "not_found", "refused", "wrong_answer"]
REASONS: tuple[Reason, ...] = ("no_answer", "server_error", "not_found", "refused", "wrong_answer")

# Our own words for each reason (the same as the alerts).
REASON_WORDS: Mapping[str, str] = {
    "no_answer": "did not answer",
    "server_error": "server error",
    "not_found": "page not found",
    "refused": "refused the request",
    "wrong_answer": "answered as something else",
}

# True of any address; used when a part's guide does not cover a reason.
GENERAL: Mapping[str, tuple[str, ...]] = {
    "no_answer": (
        "Open the address in a browser: if it loads, the outage may be the checker's path to it "
        "(compare the laptop probe with the outside checker).",
        "Check the hosting provider's status page and the DNS record for the name.",
        "Check whether the hosting account, plan or certificate has expired.",
    ),
    "server_error": (
        "Read the service's own logs for the first error around the time the alert began.",
        "Look for a deploy or a setting change just before the alert (the Code card, the hosting dashboard).",
        "If a recent deploy caused it, propose rolling back to the version before it.",
    ),
    "not_found": (
        "Check that the deployment or site still exists and the custom domain still points at it.",
        "Check that the checked path still exists after the last deploy.",
    ),
    "refused": (
        "A 4xx answer: check the edge's security events (firewall, rate limits, bot rules) for the checker.",
        "Check that the page does not now need a sign-in or a different path.",
    ),
    "wrong_answer": (
        "Something answered, but not the way this service does: check which deployment or Worker "
        "now owns the address (a different site or service may have replaced it).",
        "Look for a deploy just before the alert, in this project or one sharing its hosting account.",
        "Check the checked route still exists and still answers the expected code after the last deploy.",
    ),
}

_Text = Field(min_length=1, max_length=MAX_TEXT)
type Step = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT)]
type Steps = Annotated[tuple[Step, ...], Field(min_length=1, max_length=MAX_STEPS)]
type PartName = Annotated[str, StringConstraints(pattern=PART, max_length=MAX_PART)]


class Part(BaseModel):
    """One part of a product, and what to do when it is down."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    serves: str = _Text
    depends_on: tuple[Step, ...] = Field(default=(), max_length=MAX_STEPS)
    logs: str = _Text
    deploy: str = _Text
    rollback: str = _Text
    owner: Literal["chairman", "claude"]
    fixes: dict[Reason, Steps] = Field(default_factory=dict)


class Project(BaseModel):
    """One project's parts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    parts: dict[PartName, Part] = Field(min_length=1)


class FixGuideError(Exception):
    """The fix guides could not be used; the message says why in plain words."""


def load_fixes(path: Path, projects: Collection[str]) -> dict[str, Project]:
    """Every project's guide in ``path``; each must be a known project, each part a valid part name."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise FixGuideError(f"cannot read {path.name}") from None
    if len(raw) > MAX_FILE_BYTES:
        raise FixGuideError(f"{path.name} is larger than {MAX_FILE_BYTES // 1024} KB")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise FixGuideError(f"{path.name} is not valid YAML") from None
    if not isinstance(data, dict) or not data:
        raise FixGuideError(f"{path.name} must map each project to its parts")
    out: dict[str, Project] = {}
    for project, entry in data.items():
        if project not in projects:
            raise FixGuideError(f"{project!s} is not a known project")
        try:
            guide = Project.model_validate(entry)
        except ValidationError as exc:
            where = ".".join(str(p) for p in exc.errors()[0]["loc"])
            raise FixGuideError(f"{project}: {where or 'the entry'} is not usable") from None
        out[project] = guide
    return out


def fixes_list(path: Path | None, projects: Collection[str]) -> dict[str, Project]:
    """The guides given with ``--fixes`` (empty without one); a broken file is said and raised."""
    if path is None:
        return {}
    log = get_logger("yaqzan.settings")
    try:
        found = load_fixes(path, projects)
    except FixGuideError as exc:
        print(f"dashboard: {exc}.")  # the exact rule, on the console only: it can quote the file
        say(log, "settings_refused", setting="fix guide")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="fix guide", entries=len(found))
    return found


def guide_text(guides: Mapping[str, Project], project: str, part: str, reason: str | None = None) -> str:
    """One part's guide in plain text: every reason, or only ``reason``."""
    wanted = [r for r in REASONS if r == reason] or list(REASONS)
    entry = guides.get(project)
    found = entry.parts.get(part) if entry is not None else None
    if entry is None or found is None:
        return f"There is no fix guide for {project}/{part}. Use the general steps:\n" + _reasons({}, wanted)
    lines = [
        f"{entry.name}: {part}",
        f"Served by: {found.serves}",
        f"Depends on: {', '.join(found.depends_on) if found.depends_on else 'nothing listed'}",
        f"Logs: {found.logs}",
        f"Deploy: {found.deploy}",
        f"Roll back: {found.rollback}",
        f"Fixes are approved by: {found.owner}",
    ]
    return "\n".join(lines) + "\n" + _reasons(found.fixes, wanted)


def _reasons(fixes: Mapping[Reason, tuple[str, ...]], wanted: Sequence[Reason]) -> str:
    out: list[str] = []
    for reason in wanted:
        own = fixes.get(reason)
        steps = own if own else GENERAL[reason]
        source = "" if own else " (general steps)"
        out.append(f'If the check says "{REASON_WORDS[reason]}"{source}:')
        out.extend(f"  {n}. {step}" for n, step in enumerate(steps, 1))
    return "\n".join(out)


def fix_guide_tool(guides: Mapping[str, Project]) -> Tool | None:
    """The chat's ``fix_guide`` tool: one part's guide, chosen as ``project/part`` (none: ``None``)."""
    choices = tuple(f"{project}/{part}" for project, entry in sorted(guides.items()) for part in entry.parts)
    if not choices:
        return None

    async def run(choice: str) -> str:
        project, part = choice.split("/", 1)
        return guide_text(guides, project, part)

    return Tool(
        name="fix_guide",
        description="Read what serves one part of a project and what to check or fix when it is down.",
        choices=choices,
        run=run,
        trusted=True,
        label="Fix guide",
        workspace_of=lambda choice: choice.split("/", 1)[0],
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Check the guides, or print one part's guide; 1 if the file or the request is not usable."""
    args = sys.argv[1:] if argv is None else list(argv)
    if not 1 <= len(args) <= 4:  # the file, then an optional project, part and reason
        print("usage: python -m tools.fix_guides projects/fixes.yaml [project part [reason]]")
        return 1
    projects = {p.id for p in SamplePortfolio(datetime.now(UTC)).projects()}
    try:
        guides = load_fixes(Path(args[0]), projects)
    except FixGuideError as exc:
        print(f"fixes: {exc}.")
        return 1
    if len(args) == 1:
        for project, entry in guides.items():
            print(f"fixes: {project}: {len(entry.parts)} parts ({', '.join(entry.parts)})")
        return 0
    if len(args) == 2:
        print("usage: python -m tools.fix_guides projects/fixes.yaml project part [reason]")
        return 1
    reason = args[3] if len(args) == 4 else None
    if reason is not None and reason not in REASONS:
        print(f"fixes: the reason must be one of {', '.join(REASONS)}.")
        return 1
    print(guide_text(guides, args[1], args[2], reason))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
