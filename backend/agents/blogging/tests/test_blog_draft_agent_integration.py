"""
Integration test for the blog draft agent with a real Ollama LLM.

Run with a live LLM (e.g. LLM_PROVIDER=ollama and Ollama/Ollama Cloud available):
  pytest agents/blogging/tests/test_blog_draft_agent_integration.py -v

Skipped when LLM_PROVIDER=dummy so CI does not require an LLM by default.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest
from blog_draft_agent import BlogDraftAgent, DraftInput, DraftOutput
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
)

from llm_service import LLMPermanentError, LLMTemporaryError, OllamaLLMClient, get_client

logger = logging.getLogger(__name__)

# Output file for review (next to this test file)
_INTEGRATION_DRAFT_OUTPUT = Path(__file__).resolve().parent / "integration_draft_output.md"
_PREVIEW_CHARS = 800


def _is_placeholder_or_minimal(draft: str) -> bool:
    """True if draft is a known placeholder or too short to be real content."""
    placeholders = (
        "# Draft\n\nNo draft was generated.",
        "# Draft\n\nAdd outline to generate a draft.",
        "# Draft\n\nAdd research document and outline to generate a draft.",
        "# Draft\n\nAdd a content plan to generate a draft.",
    )
    draft_stripped = draft.strip()
    if len(draft_stripped) < 200:
        return True
    for p in placeholders:
        if draft_stripped.startswith(p) or p in draft_stripped:
            return True
    return False


def _has_basic_blog_structure(draft: str) -> tuple[bool, str]:
    """
    Fuzzy checks that the draft looks like real blog content.
    Returns (passed, reason).
    """
    if not draft or not draft.strip():
        return False, "draft is empty"
    text = draft.strip()
    if len(text) < 300:
        return False, f"draft too short ({len(text)} chars); expected substantial content"
    if _is_placeholder_or_minimal(text):
        return False, "draft is placeholder or minimal fallback"
    # At least one markdown heading
    if not re.search(r"^#+\s+\S", text, re.MULTILINE):
        return False, "draft has no markdown heading (# or ##)"
    # Multiple sentences (rough: period, exclamation, or question)
    sentence_ends = len(re.findall(r"[.!?]\s+", text)) + (1 if text.rstrip().endswith((".", "!", "?")) else 0)
    if sentence_ends < 2:
        return False, f"draft has too few sentences (counted {sentence_ends})"
    # Reasonable word count
    words = len(text.split())
    if words < 40:
        return False, f"draft word count too low ({words}); expected at least 40"
    return True, "ok"


_skip_reason = "Integration test requires real LLM (set LLM_PROVIDER=ollama and ensure Ollama/Ollama Cloud is available)"


@pytest.mark.skipif(
    os.environ.get("LLM_PROVIDER", "").lower() == "dummy" or os.environ.get("SW_LLM_PROVIDER", "").lower() == "dummy",
    reason=_skip_reason,
)
def test_draft_agent_with_ollama_produces_real_content() -> None:
    """
    Run the draft agent with the configured Ollama LLM and verify the output
    contains real blog-like content using fuzzy rules (length, structure, sentences).
    """
    client = get_client("blog")
    if not isinstance(client, OllamaLLMClient):
        pytest.skip("LLM client is not Ollama; integration test expects Ollama")

    agent = BlogDraftAgent(
        llm_client=client,
        writing_style_guide_content=(
            "Clear, conversational prose: full thoughts in natural-length sentences (~8th grade). "
            "No em dashes. Define terms on first use."
        ),
        brand_spec_content="",
    )
    plan = ContentPlan(
        overarching_topic="Observability for production systems",
        narrative_flow="Why it matters, key practices, OpenTelemetry path, wrap-up.",
        sections=[
            ContentPlanSection(
                title="Why observability matters",
                coverage_description="Stakes for platform teams.",
                order=0,
            ),
            ContentPlanSection(
                title="Key practices: logging, tracing, metrics",
                coverage_description="Concrete practices from research.",
                order=1,
            ),
            ContentPlanSection(
                title="Getting started with OpenTelemetry",
                coverage_description="Adoption steps.",
                order=2,
            ),
            ContentPlanSection(
                title="Wrap-up and next steps",
                coverage_description="Close with one action.",
                order=3,
            ),
        ],
        title_candidates=[TitleCandidate(title="Observability essentials", probability_of_success=0.7)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=[],
        ),
    )
    draft_input = DraftInput(
        research_document=(
            "Observability helps teams understand system behavior in production. "
            "Key practices include structured logging, distributed tracing, and metrics. "
            "Many organizations adopt OpenTelemetry for vendor-neutral instrumentation."
        ),
        content_plan=plan,
        audience="Platform and SRE teams",
        tone_or_purpose="technical overview",
    )

    try:
        result = agent.run(draft_input)
    except LLMPermanentError as e:
        # e.g. 401 (no API key), 404 (model not found) – skip instead of fail
        pytest.skip(f"LLM not available for integration test: {e}")
    except LLMTemporaryError as e:
        # e.g. 5xx, timeout – skip so CI without stable LLM doesn't fail
        pytest.skip(f"LLM temporarily unavailable: {e}")

    assert isinstance(result, DraftOutput)
    assert result.draft, "draft must be non-empty"

    # Write full draft to file and log preview for review
    _INTEGRATION_DRAFT_OUTPUT.write_text(result.draft, encoding="utf-8")
    preview = result.draft[: _PREVIEW_CHARS] + ("..." if len(result.draft) > _PREVIEW_CHARS else "")
    logger.info(
        "Integration test draft (%d chars) written to %s. Preview:\n---\n%s\n---",
        len(result.draft),
        _INTEGRATION_DRAFT_OUTPUT,
        preview,
    )
    print(f"\nDraft written to {_INTEGRATION_DRAFT_OUTPUT} ({len(result.draft)} chars) for review.\n")

    passed, reason = _has_basic_blog_structure(result.draft)
    assert passed, f"draft did not meet basic content rules: {reason}"
