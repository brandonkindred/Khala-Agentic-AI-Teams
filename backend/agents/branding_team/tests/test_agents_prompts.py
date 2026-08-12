"""Fidelity tests for the ``prompt_spec``-rendered ``agents.py`` prompts.

``branding_team.prompt_spec`` (``PromptField``, ``PromptSpec``,
``render_prompt``) is the data-driven replacement for the hand-written
bullet-list prose ``system_prompt`` strings in ``agents.py``'s ``make_*``
factories, mirroring the pattern proven in ``assistant/prompts.py``
(``_PHASE_ITEMS``/``_phase_section()``). This sub-issue migrates two
representative factories — ``make_purpose_vision_writer`` and
``make_iconography_director`` — as proof. These tests pin the rendered
prompt text against the exact pre-migration hand-written strings, both at
the renderer level and at the constructed-``Agent`` level, so the migration
is provably behavior-preserving.
"""

from __future__ import annotations

from branding_team.agents import (
    _ICONOGRAPHY_DIRECTOR_PROMPT_SPEC,
    _PURPOSE_VISION_PROMPT_SPEC,
    make_iconography_director,
    make_purpose_vision_writer,
)
from branding_team.prompt_spec import PromptField, PromptSpec, render_prompt

# Captured verbatim from ``agents.py`` before ``make_purpose_vision_writer``
# was migrated to ``render_prompt``. Kept as a literal snapshot so the
# migration is provably behavior-preserving: only *how* the string is built
# changed, not its content.
_ORIGINAL_PURPOSE_VISION_PROMPT = (
    "You are a Purpose & Vision Writer. Given a branding mission, write three things:\n"
    "1. brand_purpose — why the company exists (one sentence)\n"
    "2. mission_statement — what the company does for its audience (one sentence)\n"
    "3. vision_statement — the aspirational future state (one sentence)\n"
    "Be concise, inspiring, and specific to the company."
)

# Captured verbatim from ``agents.py`` before ``make_iconography_director``
# was migrated to ``render_prompt``.
_ORIGINAL_ICONOGRAPHY_DIRECTOR_PROMPT = (
    "You are an Iconography Director. Based on the winning moodboard, define:\n"
    "1. iconography_style — describe the icon aesthetic (line weight, corner radius, fill)\n"
    "2. illustration_style — describe the illustration approach (flat, isometric, etc.)"
)


def test_purpose_vision_prompt_matches_pre_migration_text() -> None:
    assert render_prompt(_PURPOSE_VISION_PROMPT_SPEC) == _ORIGINAL_PURPOSE_VISION_PROMPT


def test_iconography_director_prompt_matches_pre_migration_text() -> None:
    assert render_prompt(_ICONOGRAPHY_DIRECTOR_PROMPT_SPEC) == _ORIGINAL_ICONOGRAPHY_DIRECTOR_PROMPT


def test_purpose_vision_writer_agent_system_prompt_matches_pre_migration_text() -> None:
    """Pins the wiring, not just the renderer: the constructed Agent's system_prompt."""
    agent = make_purpose_vision_writer()
    assert agent.system_prompt == _ORIGINAL_PURPOSE_VISION_PROMPT


def test_iconography_director_agent_system_prompt_matches_pre_migration_text() -> None:
    """Pins the wiring, not just the renderer: the constructed Agent's system_prompt."""
    agent = make_iconography_director()
    assert agent.system_prompt == _ORIGINAL_ICONOGRAPHY_DIRECTOR_PROMPT


# ---------------------------------------------------------------------------
# render_prompt / PromptSpec unit tests — exercise shapes the two proof
# agents above don't (cardinality, no-closing), so the renderer's full
# contract is covered even though only two real specs use it so far.
# ---------------------------------------------------------------------------


def test_render_prompt_omits_closing_when_absent() -> None:
    spec = PromptSpec(
        role="a Tester",
        intro="Produce one thing:",
        fields=[PromptField("thing", "the thing")],
    )
    assert render_prompt(spec) == "You are a Tester. Produce one thing:\n1. thing — the thing"


def test_render_prompt_appends_cardinality_suffix() -> None:
    spec = PromptSpec(
        role="a Tester",
        intro="Produce a list:",
        fields=[PromptField("items", "the items", cardinality="3-5")],
    )
    assert render_prompt(spec) == "You are a Tester. Produce a list:\n1. items — the items (3-5)"


def test_render_prompt_includes_closing_when_present() -> None:
    spec = PromptSpec(
        role="a Tester",
        intro="Produce one thing:",
        fields=[PromptField("thing", "the thing")],
        closing="Be concise.",
    )
    assert (
        render_prompt(spec)
        == "You are a Tester. Produce one thing:\n1. thing — the thing\nBe concise."
    )
