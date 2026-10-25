"""The model levels of the model map (ADR-033, F10): the cheapest level that does the job well.

A role pack names its level ("Model" on its job card); the gateway refuses a
call above it. Shared here so the registry (which reads packs) and the gateway
(which enforces them) mean the same numbers.
"""

from __future__ import annotations

from enum import IntEnum


class ModelLevel(IntEnum):
    """How strong (and costly) a model a job may use. Higher includes everything below."""

    CODE = 0  # plain code, no model: counting, fetching, checking, every number
    DECIDER = 1  # Laya, the small local decision model (F13): route, sort, yes/no
    LOCAL = 2  # the local model (Gemma 4 26B-A4B): drafts and summaries
    SONNET = 3  # Claude Sonnet 5.5: code review, computer use in the sandbox
    OPUS = 4  # Claude Opus 5.5: department heads, Project Leads, hard reasoning
