"""A product document cut into short passages, each under the heading it sits in (ADR-037, F12 slice 1).

Only plain text is kept. Markdown (``.md``, ``.mdx``, ``.txt``) keeps its
headings as passage titles; HTML inside it (a README's logo banner) loses its
tags. A website page (``.astro``) loses its front matter,
scripts, styles, ``{...}`` expressions (values filled in at build time, which
we cannot see: a price written as ``{PLANS.monthly}`` is simply missing) and
tags; its ``<h1>``-``<h3>`` become headings and its list items lines.

Each passage holds at most :data:`MAX_PIECE_CHARS`, cut at a paragraph where
possible, and is titled "file title > heading", so a search hit says where it
came from. A document gives at most :data:`MAX_PIECES` passages.

The text is still a document someone wrote: whoever reads a passage treats it
as data, never as instructions.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

DOC_SUFFIXES = (".md", ".mdx", ".txt", ".astro")
# Short passages: each one shown costs the server's CPU model about 7 s to read (~27 tokens a second).
MAX_PIECE_CHARS = 700
MAX_TITLE_CHARS = 200
MAX_PIECES = 200

_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_BLOCKS = re.compile(r"<(script|style)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_EXPRESSION = re.compile(r"\{[^{}]*\}")
_HEADING_TAG = re.compile(r"<h([1-3])\b[^>]*>(.*?)</h\1\s*>", re.DOTALL | re.IGNORECASE)
_ITEM = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BREAK = re.compile(r"</?(p|div|section|ul|ol|br|tr|table|article|header|footer)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_MD_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SPACES = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True)
class DocPiece:
    """One passage: where it sits (``title``) and its words."""

    title: str
    body: str


def is_doc(path: str) -> bool:
    """Whether ``path`` is a kind of document this module can read."""
    return path.lower().endswith(DOC_SUFFIXES)


def doc_pieces(path: str, text: str) -> list[DocPiece]:
    """``text`` (the file at ``path``) as titled passages, in order; none if it holds no words."""
    plain = _astro_text(text) if path.lower().endswith(".astro") else _markdown_text(text)
    sections = _sections(plain)
    name = next((heading for level, heading in sections if level == 1 and heading), None) or path
    pieces: list[DocPiece] = []
    for (_, heading), body in zip(sections, _bodies(plain), strict=True):
        title = _short(name if not heading or heading == name else f"{name} > {heading}", MAX_TITLE_CHARS)
        for chunk in _chunks(body):
            pieces.append(DocPiece(title=title, body=chunk))
            if len(pieces) == MAX_PIECES:
                return pieces
    return pieces


def _markdown_text(text: str) -> str:
    """A Markdown file's words: front matter, comments and any HTML tags taken out."""
    body = _COMMENT.sub(" ", _BLOCKS.sub(" ", _FRONT_MATTER.sub("", text)))
    return html.unescape(_TAG.sub("", body))


def _astro_text(text: str) -> str:
    """A website page's words, with its headings as Markdown headings and its list items as lines."""
    body = _FRONT_MATTER.sub("", text)
    body = _COMMENT.sub(" ", _BLOCKS.sub(" ", body))
    body = _EXPRESSION.sub("", body)
    body = _HEADING_TAG.sub(lambda m: f"\n\n{'#' * int(m.group(1))} {_TAG.sub('', m.group(2))}\n\n", body)
    body = _ITEM.sub("\n- ", body)
    body = _BREAK.sub("\n\n", body)
    return html.unescape(_TAG.sub("", body))


def _lines(text: str) -> list[tuple[int, str] | str]:
    """Each line as ``(level, heading)`` or plain text; headings inside code fences are text."""
    found: list[tuple[int, str] | str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        heading = None if fenced else _MD_HEADING.match(line)
        if heading:
            found.append((len(heading.group(1)), _clean(heading.group(2))))
        else:
            found.append(line)
    return found


def _sections(text: str) -> list[tuple[int, str]]:
    """Each section's heading (``(0, "")`` for words before the first heading)."""
    return [(0, "")] + [line for line in _lines(text) if isinstance(line, tuple)]


def _bodies(text: str) -> list[str]:
    """Each section's words, in the same order as :func:`_sections`."""
    bodies: list[list[str]] = [[]]
    for line in _lines(text):
        if isinstance(line, tuple):
            bodies.append([])
        else:
            bodies[-1].append(line)
    return ["\n".join(lines) for lines in bodies]


def _chunks(body: str) -> list[str]:
    """``body`` in passages of at most ``MAX_PIECE_CHARS``, cut between paragraphs where possible."""
    paragraphs = [p for p in (_clean(block) for block in re.split(r"\n\s*\n", body)) if p]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for part in _split_long(paragraph):
            if current and len(current) + 2 + len(part) > MAX_PIECE_CHARS:
                chunks.append(current)
                current = part
            else:
                current = f"{current}\n\n{part}" if current else part
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str) -> list[str]:
    """A paragraph longer than a passage, cut at spaces."""
    parts: list[str] = []
    rest = paragraph
    while len(rest) > MAX_PIECE_CHARS:
        cut = rest.rfind(" ", 0, MAX_PIECE_CHARS)
        cut = cut if cut > MAX_PIECE_CHARS // 2 else MAX_PIECE_CHARS
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [*parts, rest] if rest else parts


def _clean(text: str) -> str:
    """Spaces squeezed and each line trimmed; blank lines dropped."""
    lines = (_SPACES.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
