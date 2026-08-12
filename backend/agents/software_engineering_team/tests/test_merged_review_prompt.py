"""Tests for the merged architecture-consistency + side-effect-impact
prompt/schema design (``prompts.MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`` /
``models.MergedArchitectureSideEffectResponse``).

This design is not wired into any pass, the coordinator, or the Temporal
workflow yet -- these tests only verify the design itself: that the merged
prompt carries both passes' full instruction content verbatim (nothing
dropped or altered), and that the merged schema actually keeps the two
passes' findings separate and attributable to their originating pass.
"""

from __future__ import annotations

import pytest
from code_review_agent.models import (
    ArchitectureConsistencyFindingLLM,
    MergedArchitectureSideEffectResponse,
    SideEffectImpactFindingLLM,
)
from code_review_agent.prompts import (
    _ARCHITECTURE_CONSISTENCY_BODY,
    _SIDE_EFFECT_IMPACT_BODY,
    ARCHITECTURE_CONSISTENCY_PROMPT,
    JSON_OUTPUT_INSTRUCTION,
    MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT,
    SIDE_EFFECT_IMPACT_PROMPT,
)
from pydantic import ValidationError


def test_individual_prompts_still_contain_their_own_body_and_output_format():
    """The prompts.py refactor (body + output-format composition) must not
    change either pass's own standalone prompt: each still opens with its
    body and ends with its own output-format section + the shared JSON
    instruction, exactly as before the refactor."""
    assert ARCHITECTURE_CONSISTENCY_PROMPT.startswith(_ARCHITECTURE_CONSISTENCY_BODY)
    assert ARCHITECTURE_CONSISTENCY_PROMPT.endswith(JSON_OUTPUT_INSTRUCTION)
    assert '"category": "architecture" | "refactor"' in ARCHITECTURE_CONSISTENCY_PROMPT
    assert '"pre_existing": boolean' in ARCHITECTURE_CONSISTENCY_PROMPT

    assert SIDE_EFFECT_IMPACT_PROMPT.startswith(_SIDE_EFFECT_IMPACT_BODY)
    assert SIDE_EFFECT_IMPACT_PROMPT.endswith(JSON_OUTPUT_INSTRUCTION)
    assert '"pre_existing": boolean' in SIDE_EFFECT_IMPACT_PROMPT


def test_merged_prompt_carries_both_bodies_verbatim():
    """Acceptance criterion: the merged prompt includes both passes' full
    instruction content, without alteration. Since the merged prompt is
    composed from the exact same body constants as each standalone prompt,
    this is a structural guarantee, not a hand-diff."""
    assert _ARCHITECTURE_CONSISTENCY_BODY in MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
    assert _SIDE_EFFECT_IMPACT_BODY in MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
    # Delineated so the model can address both independently.
    assert "Part 1" in MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
    assert "Part 2" in MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
    arch_idx = MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT.index(_ARCHITECTURE_CONSISTENCY_BODY)
    side_idx = MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT.index(_SIDE_EFFECT_IMPACT_BODY)
    assert arch_idx < side_idx


def test_merged_prompt_output_format_describes_two_separate_keys():
    assert '"architecture_findings"' in MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
    assert '"side_effect_findings"' in MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT
    assert MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT.endswith(JSON_OUTPUT_INSTRUCTION)


def test_merged_schema_round_trips_both_finding_types():
    payload = {
        "architecture_findings": [
            {
                "severity": "high",
                "category": "architecture",
                "file_path": "backend/agents/foo.py",
                "line": 10,
                "description": "Bypasses the stated data-access layer.",
                "suggestion": "Use the repository module instead.",
                "pre_existing": False,
            }
        ],
        "side_effect_findings": [
            {
                "severity": "critical",
                "category": "side-effects",
                "file_path": "backend/agents/bar.py",
                "line": 42,
                "description": "Caller at baz.py:7 assumes the old return type.",
                "suggestion": "Update the caller to handle the new return type.",
                "pre_existing": False,
            }
        ],
    }
    parsed = MergedArchitectureSideEffectResponse.model_validate(payload)
    assert len(parsed.architecture_findings) == 1
    assert len(parsed.side_effect_findings) == 1
    assert isinstance(parsed.architecture_findings[0], ArchitectureConsistencyFindingLLM)
    assert isinstance(parsed.side_effect_findings[0], SideEffectImpactFindingLLM)


def test_architecture_finding_schema_pre_existing_defaults_false():
    """``pre_existing`` is optional on an architecture/refactor finding (like
    its side-effect counterpart), defaulting False when the model omits it."""
    finding = ArchitectureConsistencyFindingLLM.model_validate(
        {"category": "architecture", "description": "Bypasses the stated data-access layer."}
    )
    assert finding.pre_existing is False

    tagged = ArchitectureConsistencyFindingLLM.model_validate(
        {
            "category": "refactor",
            "description": "Duplicates an existing helper untouched by this change.",
            "pre_existing": True,
        }
    )
    assert tagged.pre_existing is True


def test_merged_schema_accepts_explicit_empty_lists_but_requires_both_keys():
    """Both keys are required (a reply missing one is a truncated/malformed
    response, not a legitimately empty part -- see the schema's docstring),
    but an explicit empty list for either key is still a valid "found
    nothing" outcome."""
    parsed = MergedArchitectureSideEffectResponse.model_validate(
        {"architecture_findings": [], "side_effect_findings": []}
    )
    assert parsed.architecture_findings == []
    assert parsed.side_effect_findings == []

    with pytest.raises(ValidationError):
        MergedArchitectureSideEffectResponse.model_validate({"side_effect_findings": []})
    with pytest.raises(ValidationError):
        MergedArchitectureSideEffectResponse.model_validate({"architecture_findings": []})
    with pytest.raises(ValidationError):
        MergedArchitectureSideEffectResponse.model_validate({})


def test_merged_schema_rejects_cross_part_category():
    """A finding placed under the wrong part's array must fail validation --
    proves the schema actually enforces the two finding shapes stay separate,
    not merely "any string category is accepted everywhere". Both required
    top-level keys are supplied in every case (the sibling as an explicit
    empty list) so the raised error can only come from the invalid
    ``category`` value, never from the sibling key being missing."""
    with pytest.raises(ValidationError) as exc_info:
        MergedArchitectureSideEffectResponse.model_validate(
            {
                "architecture_findings": [
                    {
                        "severity": "medium",
                        "category": "side-effects",  # invalid for Part 1
                        "description": "wrong category for this array",
                    }
                ],
                "side_effect_findings": [],
            }
        )
    assert any(err["loc"][-1] == "category" for err in exc_info.value.errors())

    with pytest.raises(ValidationError) as exc_info:
        MergedArchitectureSideEffectResponse.model_validate(
            {
                "architecture_findings": [],
                "side_effect_findings": [
                    {
                        "severity": "medium",
                        "category": "refactor",  # invalid for Part 2
                        "description": "wrong category for this array",
                    }
                ],
            }
        )
    assert any(err["loc"][-1] == "category" for err in exc_info.value.errors())


def test_architecture_body_allows_review_without_formal_document():
    """Architecture findings may come from established codebase structure,
    not only from an explicit architecture document."""
    body = _ARCHITECTURE_CONSISTENCY_BODY.lower()
    assert "architecture document" in body
    assert "no formal" in body or "without a formal" in body or "when none is provided" in body
    assert "repository" in body
    assert "pattern" in body or "boundaries" in body


def test_architecture_body_documents_pre_existing_tagging() -> None:
    """Regression test: Part 1 (architecture-consistency / cross-codebase
    redundancy) must instruct the model to tag pre_existing, the same as
    Part 2 (side-effect impact) already does -- otherwise every architecture/
    refactor finding about code this submission never touched defaults to
    pre_existing=False and gets posted as a blocking PR comment instead of
    routed to a human-review proposal."""
    assert "pre_existing" in _ARCHITECTURE_CONSISTENCY_BODY
    assert "Tagging" in _ARCHITECTURE_CONSISTENCY_BODY


def test_architecture_body_prefers_scoped_reads_over_whole_file_first() -> None:
    """The architecture-consistency body must document find_references and the
    scoped construct readers as the default path, and must no longer instruct
    "you MUST use list_files()/read_file()" as the way to confirm a duplicate
    exists elsewhere in the repository."""
    assert "find_references" in _ARCHITECTURE_CONSISTENCY_BODY
    assert "read_lines" in _ARCHITECTURE_CONSISTENCY_BODY
    assert "read_function" in _ARCHITECTURE_CONSISTENCY_BODY
    assert (
        "you MUST use `list_files()`/`read_file()` to confirm" not in _ARCHITECTURE_CONSISTENCY_BODY
    )
    assert "default path" in _ARCHITECTURE_CONSISTENCY_BODY.lower()


def test_side_effect_body_prefers_scoped_reads_over_whole_file_first() -> None:
    """The side-effect-impact body must document find_references and the
    scoped construct readers as the default path for finding callers, and
    must mark read_file/list_files as the non-default fallback rather than
    the primary way to find every caller of a changed function."""
    assert "find_references" in _SIDE_EFFECT_IMPACT_BODY
    assert "read_lines" in _SIDE_EFFECT_IMPACT_BODY
    assert "read_function" in _SIDE_EFFECT_IMPACT_BODY
    assert "default path" in _SIDE_EFFECT_IMPACT_BODY.lower()
    assert "non-default" in _SIDE_EFFECT_IMPACT_BODY.lower()
    assert (
        "Use `search_codebase`/`search_repository`/`list_files`/`read_file` to find every caller"
        not in _SIDE_EFFECT_IMPACT_BODY
    )
