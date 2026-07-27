"""Coverage tests for the ``generate_structured`` pilot schema.

``ChunkReviewLLMResponse``/``ChunkReviewIssueLLM`` (code_review_agent/models.py)
are the pilot Pydantic schema for migrating ``chunk_reviewer._run_chunk_review``
to ``generate_structured``. No dedicated historical-output fixture corpus
exists anywhere in ``software_engineering_team`` (confirmed by repo search), so
these tests validate the schema against the hand-authored sample LLM-response
payloads already embedded in ``test_chunk_reviewer.py`` and
``test_code_review_coordinator.py`` -- the closest available stand-in for real
historical output.
"""

from __future__ import annotations

import pytest
from code_review_agent.models import ChunkReviewIssueLLM, ChunkReviewLLMResponse
from pydantic import ValidationError


def test_full_payload_matches_test_chunk_reviewer_markdown_fenced_sample() -> None:
    """The schema accepts the exact payload used by
    ``test_chunk_reviewer.test_chunk_review_recovers_markdown_fenced_model_response``."""
    payload = {
        "approved": False,
        "issues": [
            {
                "severity": "high",
                "category": "general",
                "file_path": "app/main.py",
                "description": "Missing input validation",
                "suggestion": "Validate before use",
            }
        ],
        "summary": "Found one issue.",
        "spec_compliance_notes": "",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.approved is False
    assert len(parsed.issues) == 1
    assert parsed.issues[0].description == "Missing input validation"
    assert parsed.issues[0].severity == "high"
    assert parsed.summary == "Found one issue."


def test_minimal_payload_defaults_missing_fields() -> None:
    """Matches ``test_missing_new_output_fields_default_to_empty``: a model
    reply that omits ``spec_compliance_notes`` still validates, defaulting it
    to an empty string (mirrors ``_run_chunk_review``'s
    ``str(data.get("spec_compliance_notes", "") or "")`` fallback)."""
    payload = {"approved": True, "issues": [], "summary": "ok"}
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.spec_compliance_notes == ""
    assert parsed.issues == []


def test_issue_missing_file_path_defaults_to_blank() -> None:
    """Matches ``test_chunk_review_agent_passes_blank_file_path_through_unchanged``:
    an issue with no ``file_path`` key validates, defaulting to ``""`` rather
    than being fabricated from the chunk label."""
    payload = {
        "approved": False,
        "issues": [
            {
                "severity": "high",
                "category": "naming",
                "description": "Use snake_case",
                "suggestion": "Rename to get_user",
            }
        ],
        "summary": "Fix naming.",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.issues[0].file_path == ""


def test_multi_issue_payload_matches_test_code_review_coordinator_sample() -> None:
    """The schema accepts a realistic multi-field issue payload matching the
    one ``test_code_review_coordinator`` scripts for its "critical issue"
    scenario, except for the out-of-set ``category`` (covered separately
    below -- that value is deliberately off-list in the source test)."""
    payload = {
        "approved": False,
        "issues": [
            {
                "severity": "medium",
                "category": "logic",
                "file_path": "app/main.py",
                "line": 12,
                "description": "Off-by-one in the loop bound",
                "suggestion": "Use range(len(items)) instead of range(len(items) + 1)",
                "pre_existing": False,
            }
        ],
        "summary": "One logic issue.",
        "spec_compliance_notes": "Meets the spec otherwise.",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.issues[0].line == 12
    assert parsed.issues[0].pre_existing is False


def test_out_of_set_category_is_rejected_by_the_stricter_schema() -> None:
    """``test_code_review_coordinator``'s "critical SQL injection" sample uses
    ``category: "security"``, a value not in the review prompt's documented
    category set (``code_review_agent/profiles.py``, mirrored by
    ``chunking._VALID_CATEGORIES``). Today's hand-rolled parsing accepts this
    silently at the ``_run_chunk_review`` level (raw dict passthrough) and
    only coerces it to "general" later, in ``chunking._issues_from_chunk_output``.

    This is the concrete, real-world-observed case motivating the pilot
    schema's stricter ``Literal`` category field: a ``generate_structured``
    call using this schema would instead fail validation here and drive one
    corrective retry, surfacing the mismatch to the model immediately instead
    of silently discarding it two hops downstream.
    """
    payload = {
        "approved": False,
        "issues": [
            {
                "severity": "critical",
                "category": "security",
                "file_path": "app/main.py",
                "description": "SQL injection risk",
                "suggestion": "Use parameterized queries",
            }
        ],
        "summary": "Critical issue found.",
    }
    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate(payload)


def test_out_of_set_severity_is_rejected_by_the_stricter_schema() -> None:
    """Matches ``test_code_review_coordinator``'s "unknown sev" sample
    (``severity: "blocker"``), which today's ``chunking._issues_from_chunk_output``
    silently coerces to "high". The pilot schema rejects it instead."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate({"description": "d", "severity": "blocker"})


def test_defaults_match_current_hand_rolled_fallbacks() -> None:
    """An empty top-level response validates with the exact defaults
    ``_run_chunk_review`` falls back to when the model reply omits every
    field: ``approved=False``, ``issues=[]``, ``summary=""``,
    ``spec_compliance_notes=""``."""
    parsed = ChunkReviewLLMResponse.model_validate({})
    assert parsed.approved is False
    assert parsed.issues == []
    assert parsed.summary == ""
    assert parsed.spec_compliance_notes == ""

    issue = ChunkReviewIssueLLM.model_validate({})
    assert issue.severity == "high"
    assert issue.category == "general"
    assert issue.file_path == ""
    assert issue.line is None
    assert issue.start_line is None
    assert issue.description == ""
    assert issue.suggestion == ""
    assert issue.pre_existing is False


def test_json_schema_renders_for_generate_structured() -> None:
    """Sanity check that the schema is usable as ``complete_validated``'s
    corrective-retry payload (``schema.model_json_schema()``) once the
    separate wiring issue lands -- this issue only designs the schema."""
    schema = ChunkReviewLLMResponse.model_json_schema()
    assert schema["properties"]["approved"]["type"] == "boolean"
    assert "issues" in schema["properties"]
