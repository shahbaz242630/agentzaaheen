"""GitHub connector: read-only code health of one repository (METRICS §6).

What it does:
    ``GitHubSource`` reads one repository's main branch, counts its open
    issues and open pull requests (search totals, never the items), and works
    out how the newest commit on the main branch built across its workflows,
    through the ``JsonGetter`` the core hands it. ``FakeGitHubSource`` serves a
    fixed shape for tests.

Public interface:
    ``GitHubSource``, ``FakeGitHubSource``, ``BASE_URL``, ``ALLOWED_HOSTS``,
    ``API_HEADERS``, ``SOURCE``. Manifest: ``plugin.yaml`` next to this file.

How to test:
    ``uv run pytest tests/unit/test_github_mapping.py tests/contract/test_repo_source_contract.py``
    (a pretend GitHub; no real network).

Must never:
    Call anything but its named GET endpoints; read an issue title, a body, a
    name or a commit message (people write those, so they are untrusted);
    hold or log the token (the core's client carries it); guess a count
    GitHub says is incomplete.
"""

from yaqzan.plugins.connectors.github.adapter import ALLOWED_HOSTS, API_HEADERS, BASE_URL, GitHubSource
from yaqzan.plugins.connectors.github.fake import FakeGitHubSource
from yaqzan.plugins.connectors.github.mapping import SOURCE

__all__ = ["ALLOWED_HOSTS", "API_HEADERS", "BASE_URL", "SOURCE", "FakeGitHubSource", "GitHubSource"]
