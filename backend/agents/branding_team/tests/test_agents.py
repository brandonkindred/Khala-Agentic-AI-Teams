"""Snapshot-fidelity tests for migrated branding agent system prompts.

Locks the exact rendered ``system_prompt`` of every factory migrated to the
data-driven ``AgentPromptSpec``/``render_agent_prompt`` pattern
(``branding_team.graphs.prompt_spec``) against the original hand-written
prose, so an accidental wording change in a spec constant is caught here
rather than silently drifting.
"""

from __future__ import annotations

from branding_team.agents import make_iconography_director, make_purpose_vision_writer

_EXPECTED_PURPOSE_VISION_PROMPT = (
    "You are a Purpose & Vision Writer. Given a branding mission, write three things:\n"
    "1. brand_purpose — why the company exists (one sentence)\n"
    "2. mission_statement — what the company does for its audience (one sentence)\n"
    "3. vision_statement — the aspirational future state (one sentence)\n"
    "Be concise, inspiring, and specific to the company."
)

_EXPECTED_ICONOGRAPHY_PROMPT = (
    "You are an Iconography Director. Based on the winning moodboard, define:\n"
    "1. iconography_style — describe the icon aesthetic (line weight, corner radius, fill)\n"
    "2. illustration_style — describe the illustration approach (flat, isometric, etc.)"
)


def test_purpose_vision_writer_prompt_matches_original_wording() -> None:
    assert make_purpose_vision_writer().system_prompt == _EXPECTED_PURPOSE_VISION_PROMPT


def test_iconography_director_prompt_matches_original_wording() -> None:
    assert make_iconography_director().system_prompt == _EXPECTED_ICONOGRAPHY_PROMPT
