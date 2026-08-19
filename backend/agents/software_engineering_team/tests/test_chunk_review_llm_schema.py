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
from code_review_agent.models import (
    ChunkReviewIssueLLM,
    ChunkReviewLLMResponse,
    _normalized_severity,
)
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


def test_missing_top_level_field_is_rejected_by_the_stricter_schema() -> None:
    """``test_missing_new_output_fields_default_to_empty`` (test_chunk_reviewer.py)
    shows today's hand-rolled parsing tolerates an ``_run_chunk_review`` reply
    that omits ``spec_compliance_notes``, defaulting it to ``""``. The prompt's
    own output-contract reminder (``FINAL_OUTPUT_CONTRACT_NOTE``) tells the
    model to always emit all four top-level keys, so a reply missing one is a
    truncated/malformed response, not a legitimately empty field: the pilot
    schema requires all four and rejects this payload instead of silently
    defaulting, so a real truncated response would drive
    ``complete_validated``'s corrective retry rather than looking like a
    clean, empty-issue approval."""
    payload = {"approved": True, "issues": [], "summary": "ok"}
    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate(payload)


def test_issue_missing_file_path_defaults_to_blank() -> None:
    """Matches ``test_chunk_review_agent_passes_blank_file_path_through_unchanged``:
    an issue with no ``file_path`` key validates, defaulting to ``""`` rather
    than being fabricated from the chunk label. All four top-level fields are
    present so only the per-issue ``file_path`` default is under test here."""
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
        "spec_compliance_notes": "",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.issues[0].file_path == ""


def test_multi_issue_payload_matches_test_code_review_coordinator_sample() -> None:
    """The schema accepts a realistic multi-field issue payload matching the
    shape ``test_code_review_coordinator`` scripts for its rejection
    scenarios (line/pre_existing populated), except severity is bumped to
    "high" here: the review prompt requires a rejection's issues to include
    at least one critical/high finding (see
    ``test_approved_false_requires_a_populated_critical_or_high_issue``), so
    a medium-only rejection -- the coordinator-level sample this shape is
    based on -- is itself an example of the malformed reply that invariant
    now catches, not a valid payload for this top-level schema."""
    payload = {
        "approved": False,
        "issues": [
            {
                "severity": "high",
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
        "spec_compliance_notes": "",
    }
    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate(payload)


def test_out_of_set_severity_is_rejected_by_the_stricter_schema() -> None:
    """Matches ``test_code_review_coordinator``'s "unknown sev" sample
    (``severity: "blocker"``), which today's ``chunking._issues_from_chunk_output``
    silently coerces to "high". The pilot schema rejects it instead."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate({"description": "d", "severity": "blocker"})


def test_numeric_pre_existing_is_rejected_by_strict_bool() -> None:
    """``pre_existing`` is ``StrictBool``: Pydantic's default lax ``bool``
    coercion would silently accept a numeric ``1`` and turn it into a real
    ``True``, erasing the distinction ``chunking._coerce_bool`` deliberately
    preserves downstream -- its policy is that a bare number is always
    false, precisely so a stray numeric value is never misread as an
    affirmative flag. Strict typing rejects the numeric value outright
    instead of silently coercing it before it ever reaches that check."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate({"description": "d", "pre_existing": 1})


def test_numeric_omission_is_rejected_by_strict_bool() -> None:
    """``omission`` is ``StrictBool`` for the same reason as ``pre_existing``
    (see ``test_numeric_pre_existing_is_rejected_by_strict_bool``): a stray
    numeric value must never be silently coerced into an affirmative flag."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate({"description": "d", "omission": 1})


def test_omission_flag_round_trips_true() -> None:
    """A model-set ``omission: true`` survives schema validation unchanged
    when paired with ``pre_existing: false`` (the only valid combination --
    see ``test_omission_and_pre_existing_both_true_is_rejected``)."""
    issue = ChunkReviewIssueLLM.model_validate(
        {"description": "d", "omission": True, "pre_existing": False}
    )
    assert issue.omission is True
    assert issue.pre_existing is False


def test_omission_and_pre_existing_both_true_is_rejected() -> None:
    """``omission=True`` and ``pre_existing=True`` together is
    self-contradictory: an omission is by definition in-scope for this
    change (see ``CodeReviewIssue.omission``'s canonical wording).
    ``_omission_implies_in_scope`` rejects the combination, driving
    ``complete_validated``'s corrective retry the same way
    ``_require_approval_consistent_with_issues`` does for a contradictory
    ``approved``/``issues`` pair."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate(
            {"description": "d", "omission": True, "pre_existing": True}
        )


def test_empty_top_level_response_is_rejected() -> None:
    """An empty top-level response (a fully truncated reply) is rejected: all
    four fields are required, so this is a schema-validation failure rather
    than the legacy hand-rolled defaults (``approved=False``, ``issues=[]``,
    ``summary=""``, ``spec_compliance_notes=""``) it used to silently produce."""
    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate({})


def test_approved_false_requires_a_populated_critical_or_high_issue() -> None:
    """The review prompt (``profiles.py``, "CRITICAL RULES FOR REJECTION") is
    explicit: "If approved=false, the issues list MUST contain at least one
    critical or high issue. An empty issues list with approved=false is
    INVALID and will be treated as an automatic approval." Today that
    fallback lives in ``coordinator._reconcile_approval``, silently
    downgrading such a rejection to an approval. The pilot schema enforces
    the same rule at the validation boundary instead, for three ways a
    reply can fail to be "populated": no issues at all, an issue with no
    severity/description populated, and issues with only sub-critical
    severities."""
    base = {"approved": False, "summary": "Rejected.", "spec_compliance_notes": ""}

    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate({**base, "issues": []})

    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate({**base, "issues": [{}]})

    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate(
            {**base, "issues": [{"severity": "low", "description": "A minor nit."}]}
        )


def test_approved_false_with_only_a_no_op_suggestion_issue_is_rejected() -> None:
    """A fourth way a rejection can fail to be "populated": the only
    critical/high issue's suggestion is, in its entirety, a no-op admission
    (``is_no_op_suggestion``, e.g. "No changes needed."). Downstream,
    ``chunking._issues_from_chunk_output`` drops exactly this issue
    (chunking.py:531-533) before ``_reconcile_approval`` ever sees it, so
    without this check the schema would accept a rejection that normalization
    silently turns into an approval anyway -- defeating the validator's whole
    purpose."""
    payload = {
        "approved": False,
        "issues": [
            {
                "severity": "critical",
                "description": "SQL injection risk",
                "suggestion": "No changes needed.",
            }
        ],
        "summary": "Rejected.",
        "spec_compliance_notes": "",
    }
    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate(payload)


def test_approved_false_with_a_populated_high_issue_is_accepted() -> None:
    """The mirror-image positive case: a rejection with a real critical/high,
    non-blank-description issue validates cleanly."""
    payload = {
        "approved": False,
        "issues": [{"severity": "critical", "description": "SQL injection risk"}],
        "summary": "Rejected.",
        "spec_compliance_notes": "",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.approved is False


def test_approved_true_with_no_issues_is_unaffected() -> None:
    """A clean approval with no issues at all is still perfectly valid."""
    payload = {
        "approved": True,
        "issues": [],
        "summary": "Looks good.",
        "spec_compliance_notes": "",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.approved is True


def test_approved_true_with_an_actionable_critical_issue_is_rejected() -> None:
    """The consistency check runs both directions: the review prompt
    (``profiles.py``) is just as explicit that APPROVE requires no
    critical/high issues as it is that REJECT requires one. Today
    ``coordinator._reconcile_approval`` silently flips this exact
    contradiction to a rejection downstream (its own
    ``approved = llm_approved and not critical_or_high``); the schema now
    catches it at the validation boundary instead."""
    payload = {
        "approved": True,
        "issues": [{"severity": "high", "description": "Missing auth check"}],
        "summary": "Looks good.",
        "spec_compliance_notes": "",
    }
    with pytest.raises(ValidationError):
        ChunkReviewLLMResponse.model_validate(payload)


def test_mixed_case_severity_is_rejected_by_issue_literal_before_consistency_check() -> None:
    """ChunkReviewIssueLLM.severity is a lowercase Literal, so mixed-case never
    reaches the after-validator. Coordinator ``CodeReviewIssue`` (free str) is
    where case-insensitive blocking matters; this locks the schema boundary."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate(
            {"severity": "HIGH", "description": "SQL injection risk"}
        )


def test_normalized_severity_helper_matches_blocking_fold_used_by_validator() -> None:
    """Guard the shared fold the consistency check relies on."""
    assert _normalized_severity("HIGH") == "high"
    assert _normalized_severity(" critical ") == "critical"


def test_approved_true_with_only_a_non_actionable_critical_issue_is_accepted() -> None:
    """The approval-side check only fires for an *actionable* critical/high
    issue -- one that would survive ``chunking._issues_from_chunk_output``'s
    own filtering. An issue with a no-op suggestion never reaches
    ``_reconcile_approval``'s ``critical_or_high`` computation downstream, so
    it must not block an approval here either."""
    payload = {
        "approved": True,
        "issues": [
            {
                "severity": "critical",
                "description": "Looked fine on closer read",
                "suggestion": "No changes needed.",
            }
        ],
        "summary": "Looks good.",
        "spec_compliance_notes": "",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.approved is True


def test_approved_true_with_only_medium_severity_issues_is_accepted() -> None:
    """Medium/low/info issues are explicitly acceptable alongside an
    approval per the review prompt ("Medium/low/info issues are
    acceptable"); only critical/high blocks an approval."""
    payload = {
        "approved": True,
        "issues": [{"severity": "medium", "description": "Minor style nit"}],
        "summary": "Looks good overall.",
        "spec_compliance_notes": "",
    }
    parsed = ChunkReviewLLMResponse.model_validate(payload)
    assert parsed.approved is True
    assert len(parsed.issues) == 1


def test_issue_defaults_match_current_hand_rolled_fallbacks() -> None:
    """An empty per-issue dict validates with the exact defaults
    ``chunking._issues_from_chunk_output`` falls back to for an omitted
    field. Unlike the top-level response, individual issue fields stay
    optional/defaulted -- an issue that omits, say, ``pre_existing`` is a
    legitimately incomplete-but-usable finding, not a truncated reply."""
    issue = ChunkReviewIssueLLM.model_validate({})
    assert issue.severity == "high"
    assert issue.category == "general"
    assert issue.file_path == ""
    assert issue.line is None
    assert issue.start_line is None
    assert issue.description == ""
    assert issue.suggestion == ""
    assert issue.pre_existing is False
    assert issue.omission is False


def test_json_schema_renders_for_generate_structured() -> None:
    """Sanity check that the schema is usable as ``complete_validated``'s
    corrective-retry payload (``schema.model_json_schema()``) once the
    separate wiring issue lands -- this issue only designs the schema."""
    schema = ChunkReviewLLMResponse.model_json_schema()
    assert schema["properties"]["approved"]["type"] == "boolean"
    assert "issues" in schema["properties"]
