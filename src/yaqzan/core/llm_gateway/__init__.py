"""One OpenAI-compatible interface to the models (local by default, ADR-023).

What it does (so far):
    :class:`FactChat` answers a dashboard question from the facts on screen
    with one model (F05). It may be given read-only tools from a closed list
    and a short skill catalogue (F06, ADR-027/029): the model asks for a tool
    with a plain-text block (``tools``), at most a few times, and every result
    goes back tagged trusted or untrusted. Its answer is text plus the list of
    tools it used.

    The model map (F10, ADR-033): :class:`ModelGate` clears every model call
    first. A role may use only models at or below its pack's level; a cloud
    call is refused if any prompt part is customer data or looks like it,
    and must fit under its use's monthly cap and the USD 180 total
    (:class:`SpendLedger`). Costs come from the API's token counts and the
    pinned price table (``model_map``).

Public interface:
    ``FactChat``, ``ChatReply``, ``ChatModel``, ``Tool``, ``SYSTEM_PROMPT``,
    ``build_messages``, ``catalogue``; ``ModelGate``, ``Clearance``,
    ``Refusal``, ``PromptPart``, ``SpendLedger``, ``SpendBook``, ``MemoryBook``,
    ``Use``, ``Usage``, ``MODELS``, ``CAPS_USD``, ``TOTAL_STOP_USD``.

How to test:
    ``uv run pytest tests/unit/test_llm_chat.py tests/unit/test_llm_tools.py`` (a fake model; no server).
    ``uv run pytest tests/unit/test_model_map.py tests/unit/test_model_gate.py``
    ``uv run pytest tests/unit/test_cloud_check.py``.

Must never:
    Give a model a tool that acts, or a tool or argument outside the closed
    lists (actions are proposals, ADR-021 aside); pass untrusted text to a
    model without the untrusted tag; send customer data to a cloud model;
    talk HTTP itself (the model is reached only through ``core.net``).
"""

from yaqzan.core.llm_gateway.chat import (
    SYSTEM_PROMPT,
    ChatModel,
    ChatReply,
    FactChat,
    Inspect,
    build_messages,
)
from yaqzan.core.llm_gateway.cloud_check import PromptPart
from yaqzan.core.llm_gateway.model_map import MODELS, Usage
from yaqzan.core.llm_gateway.route import Clearance, ModelGate, Refusal
from yaqzan.core.llm_gateway.spend import CAPS_USD, TOTAL_STOP_USD, MemoryBook, SpendBook, SpendLedger, Use
from yaqzan.core.llm_gateway.tools import Tool, catalogue

__all__ = [
    "CAPS_USD",
    "MODELS",
    "SYSTEM_PROMPT",
    "TOTAL_STOP_USD",
    "ChatModel",
    "ChatReply",
    "Clearance",
    "FactChat",
    "Inspect",
    "MemoryBook",
    "ModelGate",
    "PromptPart",
    "Refusal",
    "SpendBook",
    "SpendLedger",
    "Tool",
    "Usage",
    "Use",
    "build_messages",
    "catalogue",
]
