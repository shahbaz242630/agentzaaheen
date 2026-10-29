"""Product documents cut into titled passages (ADR-037, F12 slice 1)."""

from __future__ import annotations

from agentzaaheen.core.sdk import doc_pieces, is_doc
from agentzaaheen.core.sdk.docs import MAX_PIECE_CHARS, MAX_PIECES, MAX_TITLE_CHARS

PAGE = """---
import Base from '../layouts/Base.astro';
const page = pageFor('/products/');
---
<Base page={page}>
  <style>.card { color: red; }</style>
  <script>console.log("never read")</script>
  <!-- a note for developers -->
  <section class="page">
    <h1>Products</h1>
    <p>Zaaheen builds software for people who use AI every day.</p>
    <h2 class="x">What the memory app does</h2>
    <ul>
      <li><strong>One memory</strong> for all your AI apps.</li>
      <li>It stays on your computer &amp; is encrypted.</li>
      <li>{TRIAL.days} days free, then {PLANS.monthly}.</li>
    </ul>
  </section>
</Base>
"""


def test_a_website_page_keeps_its_words_and_headings_only() -> None:
    pieces = doc_pieces("site/src/pages/products.astro", PAGE)
    assert [p.title for p in pieces] == ["Products", "Products > What the memory app does"]
    assert pieces[0].body == "Zaaheen builds software for people who use AI every day."
    body = pieces[1].body
    assert "- One memory for all your AI apps." in body
    assert "It stays on your computer & is encrypted." in body
    text = " ".join(p.body for p in pieces)
    for gone in ("import", "console.log", "color: red", "developers", "{", "TRIAL", "<"):
        assert gone not in text
    assert "days free, then ." in body  # a value filled in at build time is simply missing


def test_markdown_headings_title_the_passages_and_code_fences_are_not_headings() -> None:
    text = (
        "# Sandoq Kin\n\nA family organiser.\n\n## Features\n\nVaults.\n\n"
        "```\n# not a heading\n```\n\n### Sharing\n\nWith family."
    )
    pieces = doc_pieces("README.md", text)
    assert [(p.title, p.body) for p in pieces] == [
        ("Sandoq Kin", "A family organiser."),
        ("Sandoq Kin > Features", "Vaults.\n\n# not a heading"),
        ("Sandoq Kin > Sharing", "With family."),
    ]


def test_without_a_top_heading_the_path_names_the_document() -> None:
    pieces = doc_pieces("docs/intro.md", "Words first.\n\n## Part\n\nMore.")
    assert [p.title for p in pieces] == ["docs/intro.md", "docs/intro.md > Part"]


def test_front_matter_is_dropped_from_markdown() -> None:
    pieces = doc_pieces("a.mdx", "---\ntitle: x\nsecret_flag: y\n---\n# A\n\nBody.")
    assert [(p.title, p.body) for p in pieces] == [("A", "Body.")]


def test_long_sections_are_cut_between_paragraphs_then_at_spaces() -> None:
    paragraph = "word " * 100  # 500 characters
    long_one = "x" * (MAX_PIECE_CHARS * 2 + 10)
    pieces = doc_pieces("a.md", f"# A\n\n{paragraph}\n\n{paragraph}\n\n{long_one}")
    assert all(len(p.body) <= MAX_PIECE_CHARS for p in pieces)
    assert pieces[0].body == paragraph.strip()  # two paragraphs do not fit one passage
    assert "".join(p.body for p in pieces[2:]) == long_one  # a word with no spaces is cut at the limit


def test_a_document_gives_at_most_so_many_passages_and_titles_are_capped() -> None:
    many = "\n\n".join(f"## {'T' * 300} {n}\n\nbody {n}" for n in range(MAX_PIECES + 50))
    pieces = doc_pieces("a.md", many)
    assert len(pieces) == MAX_PIECES
    assert all(len(p.title) <= MAX_TITLE_CHARS for p in pieces)


def test_a_document_with_no_words_gives_nothing() -> None:
    assert doc_pieces("a.md", "") == []
    assert doc_pieces("a.astro", "---\nx\n---\n<div>{only.code}</div>") == []


def test_only_document_files_are_read() -> None:
    assert all(is_doc(p) for p in ("README.md", "a.MDX", "notes.txt", "site/index.astro"))
    assert not any(is_doc(p) for p in ("a.ts", "logo.png", ".env", "README"))
