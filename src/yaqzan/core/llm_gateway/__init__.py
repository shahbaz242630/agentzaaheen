"""One OpenAI-compatible interface to the models (local by default, ADR-023).

What it does (so far):
    :class:`FactChat` answers a dashboard question from the facts on screen
    with one model (F05). It may be given read-only tools from a closed list
    and a short skill catalogue (F06, ADR-027/029): the model asks for a tool
    with a plain-text block (``tools``), at most a few times, and every result
    goes back tagged trusted or untrusted. Its answer is text plus the list of
    tools it used.

Public interface:
    ``FactChat``, ``ChatReply``, ``ChatModel``, ``Tool``, ``SYSTEM_PROMPT``,
    ``build_messages``, ``catalogue``.

How to test:
    ``uv run pytest tests/unit/test_llm_chat.py tests/unit/test_llm_tools.py`` (a fake model; no server).

Must never:
    Give a model a tool that acts, or a tool or argument outside the closed
    lists (actions are proposals, ADR-021 aside); pass untrusted text to a
    model without the untrusted tag; send customer data to a cloud model;
    talk HTTP itself (the model is reached only through ``core.net``).
"""

from yaqzan.core.llm_gateway.chat import SYSTEM_PROMPT, ChatModel, ChatReply, FactChat, build_messages
from yaqzan.core.llm_gateway.tools import Tool, catalogue

__all__ = ["SYSTEM_PROMPT", "ChatModel", "ChatReply", "FactChat", "Tool", "build_messages", "catalogue"]
