"""Reading one skill folder: ``SKILL.md`` in the open Agent Skills format (ADR-022, research/07).

The header may hold only the fields the format allows (the same list
Anthropic's validator checks), so our skills stay usable in Claude, Codex and
Cursor. Agent Zaaheen's own fields live under ``metadata`` as plain text, which is
what the format allows there:

    metadata:
      requires: github          # connector ids, comma-separated
      invocation: manual        # auto (default) or manual

Claude Code's ``disable-model-invocation: true`` also means manual.
"""

from __future__ import annotations

import re
from pathlib import Path

from yaqzan.core.registry.files import (
    ItemError,
    fingerprint,
    lenient_mapping,
    read_text,
    refuse_secrets,
    split_frontmatter,
)
from yaqzan.core.registry.models import ITEM_ID, Invocation, Skill

SKILL_FILE = "SKILL.md"
ALLOWED_FIELDS = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "compatibility",
        "disable-model-invocation",
    }
)
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 500
_ID = re.compile(ITEM_ID)


def read_skill(folder: Path) -> Skill:
    """The skill in ``folder``, or :class:`ItemError` saying why it was refused."""
    path = folder / SKILL_FILE
    if not path.is_file():
        raise ItemError("registry.no_manifest", f"the folder has no {SKILL_FILE}")
    header, body = split_frontmatter(read_text(path), SKILL_FILE)
    fields = lenient_mapping(header, SKILL_FILE)
    refuse_secrets(fields, SKILL_FILE)
    unknown = sorted(set(fields) - ALLOWED_FIELDS)
    if unknown:
        raise ItemError(
            "registry.unknown_field",
            f"{SKILL_FILE} has fields the skill format does not allow: {', '.join(unknown)} "
            "(Agent Zaaheen's own fields go under 'metadata')",
        )
    name = _name(fields.get("name"), folder.name)
    metadata = _metadata(fields.get("metadata"))
    lines = body.strip().split("\n")
    if not body.strip():
        raise ItemError("registry.empty_skill", f"{SKILL_FILE} has a header but no instructions")
    if len(lines) > MAX_BODY_LINES:
        raise ItemError(
            "registry.too_long", f"{SKILL_FILE} is over {MAX_BODY_LINES} lines; move detail to files"
        )
    return Skill(
        id=name,
        description=_description(fields.get("description")),
        invocation=_invocation(metadata.get("invocation"), fields.get("disable-model-invocation")),
        requires=_requires(metadata.get("requires")),
        fingerprint=fingerprint(folder),
    )


def _name(value: object, folder: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_NAME or not _ID.match(value):
        raise ItemError(
            "registry.bad_name",
            "the skill's name must be lowercase words joined by single hyphens, at most 64 characters",
        )
    if value != folder:
        raise ItemError(
            "registry.name_mismatch", f"the skill is named '{value}' but its folder is '{folder}'"
        )
    return value


def _description(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ItemError("registry.no_description", "the skill needs a one-line description")
    text = " ".join(value.split())
    if len(text) > MAX_DESCRIPTION:
        raise ItemError("registry.bad_description", f"the description is over {MAX_DESCRIPTION} characters")
    if "<" in text or ">" in text:
        raise ItemError("registry.bad_description", "the description may not contain '<' or '>'")
    return text


def _metadata(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ItemError("registry.bad_metadata", "'metadata' must map names to plain text values")
    return value


def _invocation(value: str | None, model_disabled: object) -> Invocation:
    if model_disabled is True:
        return Invocation.MANUAL
    if model_disabled not in {None, False}:
        raise ItemError("registry.bad_metadata", "'disable-model-invocation' must be true or false")
    if value is None:
        return Invocation.AUTO
    try:
        return Invocation(value.strip().lower())
    except ValueError:
        raise ItemError("registry.bad_metadata", "'invocation' must be 'auto' or 'manual'") from None


def _requires(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    ids = tuple(part.strip() for part in value.split(","))
    if any(not _ID.match(part) for part in ids):
        raise ItemError("registry.bad_metadata", "'requires' must list connector ids separated by commas")
    return tuple(dict.fromkeys(ids))


def skill_instructions(folder: Path, expected_fingerprint: str) -> str:
    """The skill's instructions (its body), only if its files are exactly as when it was loaded.

    A change since then (an edit, a new file) means it has not been reviewed
    in that form, so it is refused rather than handed to a model.
    """
    if fingerprint(folder) != expected_fingerprint:
        raise ItemError(
            "registry.changed", "the skill's files changed since it was loaded; restart to review it"
        )
    _, body = split_frontmatter(read_text(folder / SKILL_FILE), SKILL_FILE)
    return body.strip()
