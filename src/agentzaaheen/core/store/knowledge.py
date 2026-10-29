"""The knowledge bank's passages, per workspace (migration 0020, ADR-037, F12 slice 1).

A document is kept as passages under its path and git blob id. When the file
changes, all its passages are replaced in one transaction; when it leaves the
list, they go. Search is Postgres full-text search over each passage's
``words``: any of the question's words may match (an "or" search, ranked by
how well each passage matches), because a question names a few words and a
passage rarely holds all of them; a heading's words count more. With an
``overview`` document (the project's first listed one), its first real
passage always comes first, so even a vague question ("what is this for?")
sees what the product is. Too few hits are topped up with documents' openings.

Log lines name only the kind of record and counts, never the text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from agentzaaheen.core.store.db import Scoped, require_aware, written

PRODUCTS = "products"
MAX_RESULTS = 8
SHOWN = 3  # passages a chat question gets: ~525 tokens, about 20 s on the server's CPU model

_KNOWN = """
SELECT source, blob_sha FROM agentzaaheen.knowledge_pieces
WHERE tenant_id = $1 AND project_id = $2 AND section = $3 AND position = 0
"""
_DROP = """
DELETE FROM agentzaaheen.knowledge_pieces
WHERE tenant_id = $1 AND project_id = $2 AND section = $3 AND source = $4
"""
_ADD = """
INSERT INTO agentzaaheen.knowledge_pieces
    (tenant_id, project_id, section, source, blob_sha, position, title, body, read_at)
SELECT $1, $2, $3, $4, $5, p.position - 1, p.title, p.body, $8
FROM unnest($6::text[], $7::text[]) WITH ORDINALITY AS p(title, body, position)
"""
# The question's own stemmed words, joined with "or"; ``simple`` so they are not stemmed twice.
_SEARCH = """
WITH asked AS (
    SELECT to_tsquery('simple', COALESCE(string_agg(quote_literal(word), ' | '), '')) AS q
    FROM unnest(tsvector_to_array(to_tsvector('english', $4))) AS word
)
SELECT source, position, title, body, read_at, ts_rank_cd(words, asked.q) AS rank
FROM agentzaaheen.knowledge_pieces, asked
WHERE tenant_id = $1 AND project_id = $2 AND section = $3 AND words @@ asked.q
ORDER BY rank DESC, source, position
LIMIT $5
"""
# The first passage of the overview document with some words in it (a title line alone is not enough).
_OVERVIEW = """
SELECT source, position, title, body, read_at, 0.0::real AS rank FROM agentzaaheen.knowledge_pieces
WHERE tenant_id = $1 AND project_id = $2 AND section = $3 AND source = $4 AND char_length(body) >= 80
ORDER BY position
LIMIT 1
"""
_OPENINGS = """
SELECT source, position, title, body, read_at, 0.0::real AS rank FROM agentzaaheen.knowledge_pieces
WHERE tenant_id = $1 AND project_id = $2 AND section = $3 AND position = 0
ORDER BY source
LIMIT $4
"""


@dataclass(frozen=True)
class Passage:
    """One passage found: the document it came from, where in it, its words, and when it was read."""

    source: str
    position: int
    title: str
    body: str
    read_at: datetime


class KnowledgeRows:
    """One workspace's passages in one section (only ``products`` exists yet)."""

    def __init__(self, scope: Scoped, section: str = PRODUCTS) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace(), section)

    async def known(self) -> dict[str, str]:
        """Each kept document's path and the git blob id it was read from."""
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_KNOWN, *self._key)
        return {row["source"]: row["blob_sha"] for row in rows}

    async def replace(
        self, source: str, blob_sha: str, pieces: Sequence[tuple[str, str]], *, at: datetime
    ) -> None:
        """Keep ``pieces`` (title, body) as ``source``'s passages, in place of any before; none drops it."""
        require_aware(at)
        titles = [title for title, _ in pieces]
        bodies = [body for _, body in pieces]
        async with self._scope.write("a document's passages") as conn:
            await conn.execute(_DROP, *self._key, source)
            if pieces:
                await conn.execute(_ADD, *self._key, source, blob_sha, titles, bodies, at)

    async def drop(self, sources: Sequence[str]) -> int:
        """Remove the passages of each document in ``sources``; how many documents had any."""
        gone = 0
        async with self._scope.write("documents removed") as conn:
            for source in sources:
                status = await conn.execute(_DROP, *self._key, source)
                gone += status != "DELETE 0"
            written(gone or None)
        return gone

    async def search(
        self, question: str, limit: int = SHOWN, *, overview: str | None = None
    ) -> list[Passage]:
        """The ``overview`` document's first passage, then the best matches for ``question``; topped up."""
        limit = max(1, min(limit, MAX_RESULTS))
        async with self._scope.connection() as conn:
            first = None if overview is None else await conn.fetchrow(_OVERVIEW, *self._key, overview)
            found = [] if first is None else [_passage(first)]
            # One more than needed, in case the overview passage is among the hits.
            for row in await conn.fetch(_SEARCH, *self._key, question[:2000], limit + 1):
                _add(found, row, limit)
            for row in await conn.fetch(_OPENINGS, *self._key, limit + 1):
                _add(found, row, limit)
        return found


def _add(found: list[Passage], row: asyncpg.Record, limit: int) -> None:
    """Add ``row`` unless there are ``limit`` already or it is there."""
    if len(found) < limit and all((p.source, p.position) != (row["source"], row["position"]) for p in found):
        found.append(_passage(row))


def _passage(row: asyncpg.Record) -> Passage:
    return Passage(
        source=row["source"],
        position=row["position"],
        title=row["title"],
        body=row["body"],
        read_at=row["read_at"],
    )
