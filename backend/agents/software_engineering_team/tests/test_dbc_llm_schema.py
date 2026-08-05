"""Coverage tests for ``DbcCommentsLLMResponse``, decoupled from the retry-loop
plumbing exercised in ``test_dbc_comments_agent.py``. Mirrors
``test_chunk_review_llm_schema.py``'s style for the analogous
``ChunkReviewLLMResponse`` schema.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsLLMResponse,
)


def test_full_payload_validates() -> None:
    payload = {
        "insertions": [
            {
                "file": "a.py",
                "symbol": "f",
                "line": 2,
                "comment": '"""Does nothing."""',
                "action": "add",
            }
        ],
        "already_compliant": False,
        "summary": "added comments",
        "suggested_commit_message": "docs(dbc): comments",
        "comments_added": 1,
        "comments_updated": 0,
    }
    parsed = DbcCommentsLLMResponse.model_validate(payload)
    assert parsed.already_compliant is False
    assert len(parsed.insertions) == 1
    assert parsed.insertions[0].file == "a.py"
    assert parsed.summary == "added comments"


def test_compliant_empty_insertions_validates() -> None:
    payload = {"insertions": [], "already_compliant": True}
    parsed = DbcCommentsLLMResponse.model_validate(payload)
    assert parsed.already_compliant is True
    assert parsed.insertions == []
    # Optional fields default rather than requiring the model to repeat them.
    assert parsed.summary == ""
    assert parsed.suggested_commit_message == "docs(dbc): add Design by Contract comments"
    assert parsed.comments_added == 0
    assert parsed.comments_updated == 0


def test_missing_insertions_is_rejected() -> None:
    """insertions is required, not defaulted: a truncated reply that omits it
    entirely must fail validation and drive complete_validated's corrective
    retry, not silently look like a clean, empty, compliant response."""
    with pytest.raises(ValidationError):
        DbcCommentsLLMResponse.model_validate({"already_compliant": True})


def test_missing_already_compliant_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DbcCommentsLLMResponse.model_validate({"insertions": []})


def test_empty_top_level_response_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DbcCommentsLLMResponse.model_validate({})


def test_non_list_insertions_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DbcCommentsLLMResponse.model_validate(
            {"insertions": "not a list", "already_compliant": False}
        )


def test_one_malformed_insertion_entry_fails_the_whole_response() -> None:
    """Unlike the legacy hand-rolled per-item parsing (which skipped a bad
    entry and kept the rest), the schema fails the WHOLE response when any
    insertion in the list is missing a required field -- there is no
    partial-tolerance path once complete_validated owns parsing."""
    payload = {
        "insertions": [
            {"file": "good.py", "symbol": "f", "comment": "docstring"},
            {"file": "missing_comment.py", "symbol": "g"},  # missing required "comment"
        ],
        "already_compliant": False,
    }
    with pytest.raises(ValidationError):
        DbcCommentsLLMResponse.model_validate(payload)


def test_non_object_top_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DbcCommentsLLMResponse.model_validate([])


def test_json_schema_renders_for_complete_validated() -> None:
    """Sanity check that the schema is usable as complete_validated's
    corrective-retry payload (schema.model_json_schema())."""
    schema = DbcCommentsLLMResponse.model_json_schema()
    assert schema["properties"]["already_compliant"]["type"] == "boolean"
    assert "insertions" in schema["properties"]
