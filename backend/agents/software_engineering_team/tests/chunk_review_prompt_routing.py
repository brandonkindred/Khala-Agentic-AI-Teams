"""Shared prompt classifiers for chunk-review test doubles.

Several coordinator-level test files each independently defined a
byte-identical ``_is_chunk_map_reasoning_prompt`` helper (one in
``test_code_review_coordinator.py``, and an identical copy in both
``test_v2_review_fallback_e2e.py`` and ``test_v2_fe_review_fallback_e2e.py``).
This module is the single source of truth those files import from instead.
"""

from __future__ import annotations

from software_engineering_team.code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER

# The prefix ``wrap_with_analysis_delimiters`` (via_reasoning.py) injects on
# every formatting-pass prompt.  Present in formatting prompts for ALL pass
# types (map, synthesis, spec-compliance), and never in any reasoning prompt.
_ANALYSIS_DELIMITER = "--- ANALYSIS"


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


def is_formatting_pass_prompt(prompt: str) -> bool:
    """True when ``prompt`` is a formatting-pass prompt (any pass type).

    ``run_agent_via_reasoning`` always wraps the reasoning prose in
    ``wrap_with_analysis_delimiters``'s "--- ANALYSIS" markers before
    passing it to ``complete_json`` for the formatting call.  This marker
    is present in formatting prompts for ALL pass types (map-chunk,
    synthesis, spec-compliance) and is never present in any reasoning
    prompt.

    Use this classifier (rather than ``is_chunk_map_reasoning_prompt``)
    when a test double needs to distinguish formatting calls from ALL
    reasoning calls — not only chunk-map reasoning calls.

    Preconditions:
        ``prompt`` is a ``complete_json`` call's raw prompt argument.

    Postconditions:
        Returns ``True`` iff ``prompt`` is a formatting-pass prompt.
        Never raises.
    """
    return _ANALYSIS_DELIMITER in prompt
