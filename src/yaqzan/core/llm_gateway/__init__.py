"""One OpenAI-compatible interface to the models (local by default, ADR-023).

What it does (so far):
    :class:`FactChat` answers a dashboard question from the facts on screen
    with one model (F05). The model has no tools; it only writes text.

Public interface:
    ``FactChat``, ``ChatModel``, ``SYSTEM_PROMPT``, ``build_messages``.

How to test:
    ``uv run pytest tests/unit/test_llm_chat.py`` (a fake model; no server).

Must never:
    Give a model tools or the power to act (actions are proposals, ADR-021
    aside); send customer data to a cloud model; talk HTTP itself (the model
    is reached only through ``core.net``).
"""

from yaqzan.core.llm_gateway.chat import SYSTEM_PROMPT, ChatModel, FactChat, build_messages

__all__ = ["SYSTEM_PROMPT", "ChatModel", "FactChat", "build_messages"]
