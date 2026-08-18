"""Shared map-phase reasoning-prompt classifier for chunk-review test doubles.

Several coordinator-level test files each independently defined a
byte-identical ``_is_chunk_map_reasoning_prompt`` helper (one in
``test_code_review_coordinator.py``, and an identical copy in both
``test_v2_review_fallback_e2e.py`` and ``test_v2_fe_review_fallback_e2e.py``).
This module is the single source of truth those files import from instead.
"""

from __future__ import annotations

from software_engineering_team.code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER


def is_chunk_map_reasoning_prompt(prompt: str) -> bool:
    """True when ``prompt`` is the map-phase chunk-review reasoning user message.

    ``_run_chunk_review`` runs the reasoning pass through a real Strands
    ``Agent`` (``run_agent_via_reasoning``); ``DummyLLMClient.chat()``
    delegates to ``complete_json`` for BOTH the reasoning pass (reached via
    the agent's ``chat()`` call) and the formatting pass (a direct
    ``complete_json`` call). Only the reasoning pass's raw prompt carries
    ``CODE_TO_REVIEW_HEADER`` -- the formatting pass's prompt is the
    reasoning prose wrapped in ``wrap_with_analysis_delimiters``'s
    "--- ANALYSIS" markers instead.

    Preconditions:
        ``prompt`` is a ``complete_json`` call's raw prompt argument.

    Postconditions:
        Returns ``True`` iff ``prompt`` is a reasoning-pass (not
        formatting-pass) chunk-review prompt. Never raises.
    """
    return CODE_TO_REVIEW_HEADER in prompt
