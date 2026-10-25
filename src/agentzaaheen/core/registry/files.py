"""Reading an item's files safely: size caps, tolerant text, safe YAML, secret refusal, fingerprints.

Lessons from research/07 §5 built in: a UTF-8 BOM and Windows line endings
are accepted; a third-party ``description: Build for AWS: ECS`` (an unquoted
colon) is retried with the value quoted instead of being refused; a file
that expands into a huge structure (a YAML alias bomb) is refused. Anything
that looks like a secret is refused outright: manifests hold slot names only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from agentzaaheen.core.sdk import contains_sensitive, is_sensitive_key

MAX_FILE_BYTES = 64 * 1024
MAX_NODES = 2_000
MAX_DEPTH = 20
MAX_ITEM_FILES = 512
MAX_ITEM_BYTES = 16 * 1024 * 1024
SKIPPED_DIRS = frozenset({"__pycache__", ".git"})

# An email is not a secret; this one ignores "user:pass@host" so URL credentials are still caught.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+:/-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")
_PLACEHOLDER = re.compile(r"^(?:[A-Za-z]{1,20} )?\$\{[^}\s]{1,128}\}$")  # "${KEY}" or "Bearer ${KEY}"
_PLAIN_PAIR = re.compile(r"^(\s*[A-Za-z0-9_-]+:\s+)(.*:\s.*)$")


class ItemError(Exception):
    """One item could not be read. ``code`` is a reason code; ``message`` says why in plain English."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def read_text(path: Path) -> str:
    """The file as text, with a BOM removed and Windows line endings made plain newlines."""
    if path.is_symlink():
        raise ItemError(
            "registry.symlink", f"{path.name} is a link to somewhere else; links are not followed"
        )
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ItemError("registry.too_big", f"{path.name} is over {MAX_FILE_BYTES // 1024} KB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ItemError("registry.not_utf8", f"{path.name} is not UTF-8 text") from None
    return text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")


def load_mapping(text: str, name: str) -> dict[str, object]:
    """``text`` parsed with ``yaml.safe_load`` (JSON is valid YAML); it must be a small mapping."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ItemError("registry.bad_yaml", f"{name} could not be parsed") from None
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise ItemError("registry.not_a_mapping", f"{name} must be a set of named fields")
    sum(1 for _ in _walk(data, "", 0))  # counts every node; refuses a structure that is too big
    return data


def lenient_mapping(text: str, name: str) -> dict[str, object]:
    """Like :func:`load_mapping`, but retries once with ``key: a: b`` values quoted."""
    try:
        return load_mapping(text, name)
    except ItemError as first:
        if first.code != "registry.bad_yaml":
            raise
        quoted = "\n".join(_quote_plain_value(line) for line in text.split("\n"))
        return load_mapping(quoted, name)


def _quote_plain_value(line: str) -> str:
    match = _PLAIN_PAIR.match(line)
    if match is None or match.group(2).lstrip()[:1] in {'"', "'", "[", "{", "|", ">"}:
        return line
    value = match.group(2).replace("\\", "\\\\").replace('"', '\\"')
    return f'{match.group(1)}"{value}"'


def split_frontmatter(text: str, name: str) -> tuple[str, str]:
    """The YAML between the opening ``---`` and closing ``---`` lines, and the body after it."""
    if not text.startswith("---\n"):
        raise ItemError("registry.no_frontmatter", f"{name} must start with a '---' header block")
    end = text.find("\n---", 3)
    while end != -1 and text[end + 4 : end + 5] not in {"\n", ""}:
        end = text.find("\n---", end + 4)
    if end == -1:
        raise ItemError("registry.no_frontmatter", f"{name} has no closing '---' after its header")
    return text[4 : end + 1], text[end + 5 :]


def refuse_secrets(data: dict[str, object], name: str) -> None:
    """Refuse ``data`` if any value looks like a secret, or a secret-named field holds a real value."""
    for key, value in _walk(data, "", 0):
        if not isinstance(value, str) or not value:
            continue
        if contains_sensitive(_EMAIL.sub("", value)) or (
            is_sensitive_key(key) and not _PLACEHOLDER.match(value)
        ):
            raise ItemError(
                "registry.secret_in_manifest",
                f"{name} holds something that looks like a secret under '{key}'; "
                "keys belong in the vault, a manifest only names the slot",
            )


def _walk(
    value: object, key: str, depth: int, _seen: list[int] | None = None
) -> Iterator[tuple[str, object]]:
    seen = [0] if _seen is None else _seen
    seen[0] += 1
    if seen[0] > MAX_NODES or depth > MAX_DEPTH:
        raise ItemError("registry.too_complex", "the file is too large or too deeply nested to read safely")
    yield key, value
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _walk(child, str(child_key), depth + 1, seen)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, key, depth + 1, seen)


def fingerprint(folder: Path) -> str:
    """SHA-256 over every file's relative path and bytes, in a fixed order (caches skipped)."""
    files = sorted(_item_files(folder), key=lambda pair: pair[0])
    if len(files) > MAX_ITEM_FILES:
        raise ItemError("registry.too_many_files", f"the folder has more than {MAX_ITEM_FILES} files")
    digest = hashlib.sha256()
    total = 0
    for relative, path in files:
        data = path.read_bytes()
        total += len(data)
        if total > MAX_ITEM_BYTES:
            raise ItemError("registry.too_big", f"the folder is over {MAX_ITEM_BYTES // (1024 * 1024)} MB")
        digest.update(relative.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return digest.hexdigest()


def _item_files(folder: Path) -> Iterator[tuple[str, Path]]:
    for path in folder.iterdir():
        if path.is_symlink():
            raise ItemError(
                "registry.symlink", f"{path.name} is a link to somewhere else; links are not followed"
            )
        if path.is_dir():
            if path.name not in SKIPPED_DIRS:
                for relative, child in _item_files(path):
                    yield f"{path.name}/{relative}", child
        elif path.suffix != ".pyc":
            yield path.name, path
