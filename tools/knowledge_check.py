"""The knowledge bank's scored test set (ADR-037, F12 slice 1): do real questions find the right passages?

Each test question names a project, the question as the Chairman would ask
it, and one or more phrases (``|`` between them) that should appear in the
passages the chat would be shown. The check searches the laptop database
exactly as the chat does (the project's overview first, then the best word
matches) and passes a question when any phrase is in those passages. It
prints each result and the score, and fails below ``--min``.

The questions live in a local file (``projects/knowledge-questions.yaml``,
git-ignored), because most product documents are in private repositories.
Run it after changing the documents, the list, the cutting or the search:

    uv run python -m tools.knowledge_check --min 10

A question marked ``known_gap`` is shown but not scored: an answer we know the
documents cannot give yet (prices filled in when the site is built).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import SecretStr

from agentzaaheen.core.store import Database, DatabaseSettings, KnowledgeRows, Passage, WorkspaceRow
from tools import laptop_db
from tools.dashboard_knowledge import LaptopKnowledge, knowledge_list

QUESTIONS = Path("projects/knowledge-questions.yaml")
LISTED = Path("projects/knowledge.yaml")


@dataclass(frozen=True)
class Question:
    """One test question and the phrases its passages should hold."""

    project: str
    question: str
    expect: tuple[str, ...]
    known_gap: bool = False


def load_questions(path: Path) -> list[Question]:
    """The test questions in ``path``; a broken file raises ``ValueError`` saying why."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list) or not loaded:
        raise ValueError(f"{path} must be a list of questions")
    questions: list[Question] = []
    for entry in loaded:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) for k in ("project", "q", "expect")
        ):
            raise ValueError(f"{path}: each question needs project, q and expect")
        questions.append(
            Question(
                project=entry["project"],
                question=entry["q"],
                expect=tuple(p.strip() for p in entry["expect"].split("|") if p.strip()),
                known_gap=entry.get("known_gap") is True,
            )
        )
    return questions


def passes(question: Question, found: Sequence[Passage]) -> bool:
    """Whether any expected phrase is in the passages found (case does not matter)."""
    text = " ".join(f"{p.title} {p.body}" for p in found).lower()
    return any(phrase.lower() in text for phrase in question.expect)


async def score(url: SecretStr, questions: Sequence[Question], listed: dict[str, tuple[str, ...]]) -> int:
    """Search every question as the chat would; print each result; the number that passed."""
    db = await Database.open(DatabaseSettings(dsn=url, min_size=1, max_size=2))
    by_slug = {w.slug: w for w in laptop_db.LAPTOP_WORKSPACES}

    async def rows(workspace: WorkspaceRow) -> KnowledgeRows:
        return KnowledgeRows(db.scoped(laptop_db.LAPTOP_TENANT, workspace.workspace_id))

    search = LaptopKnowledge(rows, listed)
    passed = 0
    try:
        for question in questions:
            workspace = by_slug.get(question.project)
            found = [] if workspace is None else await search.search(workspace, question.question)
            ok = passes(question, found)
            mark = "GAP " if question.known_gap else ("PASS" if ok else "MISS")
            passed += ok and not question.known_gap
            print(f"{mark} [{question.project}] {question.question}")
            for passage in found:
                print(f"       {passage.source} | {passage.title[:70]}")
    finally:
        await db.close()
    return passed


def main(argv: Sequence[str] | None = None) -> int:
    """Run the test set against the laptop database; 0 if the score is at least ``--min``."""
    parser = argparse.ArgumentParser(prog="knowledge_check", description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--knowledge", type=Path, default=LISTED)
    parser.add_argument("--min", type=int, required=True, help="the lowest score that passes")
    ns = parser.parse_args(argv)
    try:
        questions = load_questions(ns.questions)
        listed = knowledge_list(ns.knowledge)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"knowledge_check: {exc}")
        return 1
    url = asyncio.run(laptop_db.prepare())
    if url is None:
        print("knowledge_check: the laptop database is not running (uv run python -m tools.laptop_db up).")
        return 1
    scored = [q for q in questions if not q.known_gap]
    passed = asyncio.run(score(url, questions, listed))
    print(f"score {passed}/{len(scored)} (pass mark {ns.min})")
    return 0 if passed >= ns.min else 1


if __name__ == "__main__":
    sys.exit(main())
