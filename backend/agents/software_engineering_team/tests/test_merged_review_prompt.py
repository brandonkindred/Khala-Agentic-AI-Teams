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
    _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION,
    ARCHITECTURE_CONSISTENCY_FORMATTING_INSTRUCTIONS,
    ARCHITECTURE_CONSISTENCY_PROMPT,
    ARCHITECTURE_CONSISTENCY_REASONING_SYSTEM_PROMPT,
    JSON_OUTPUT_INSTRUCTION,
    MERGED_ARCHITECTURE_SIDE_EFFECT_FORMATTING_INSTRUCTIONS,
    MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT,
    MERGED_ARCHITECTURE_SIDE_EFFECT_REASONING_SYSTEM_PROMPT,
    SIDE_EFFECT_IMPACT_FORMATTING_INSTRUCTIONS,
    SIDE_EFFECT_IMPACT_PROMPT,
    SIDE_EFFECT_IMPACT_REASONING_SYSTEM_PROMPT,
    build_merged_architecture_side_effect_prompt,
    build_merged_architecture_side_effect_reasoning_system_prompt,
    build_side_effect_impact_reasoning_system_prompt,
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


def test_submission_pass_prompts_split_reasoning_and_formatting() -> None:
    assert ARCHITECTURE_CONSISTENCY_PROMPT == (
        ARCHITECTURE_CONSISTENCY_REASONING_SYSTEM_PROMPT
        + ARCHITECTURE_CONSISTENCY_FORMATTING_INSTRUCTIONS
    )
    assert _ARCHITECTURE_CONSISTENCY_BODY in ARCHITECTURE_CONSISTENCY_REASONING_SYSTEM_PROMPT
    assert "Return a single JSON object" in ARCHITECTURE_CONSISTENCY_FORMATTING_INSTRUCTIONS
    assert "Answer in structured prose" in ARCHITECTURE_CONSISTENCY_REASONING_SYSTEM_PROMPT

    assert SIDE_EFFECT_IMPACT_PROMPT == (
        SIDE_EFFECT_IMPACT_REASONING_SYSTEM_PROMPT + SIDE_EFFECT_IMPACT_FORMATTING_INSTRUCTIONS
    )

    assert MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT == (
        MERGED_ARCHITECTURE_SIDE_EFFECT_REASONING_SYSTEM_PROMPT
        + MERGED_ARCHITECTURE_SIDE_EFFECT_FORMATTING_INSTRUCTIONS
    )
    assert build_merged_architecture_side_effect_prompt(arch_on=True, side_on=False).startswith(
        "You are running ONLY the architecture-consistency"
    )


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


def test_side_effect_body_mutation_subcheck_present_by_default() -> None:
    """The default (mutation-on) body documents the mutation-vs-replaced-code
    contract sub-check: it's scoped to files with a shown before-image, cites
    the caller-inspection tools, and frames the verdict in DbC terms."""
    lower = _SIDE_EFFECT_IMPACT_BODY.lower()
    assert "replaced (pre-change) content" in lower
    assert "mutation" in lower
    assert "contract" in lower
    assert "find_references" in _SIDE_EFFECT_IMPACT_BODY
    assert "search_repository" in _SIDE_EFFECT_IMPACT_BODY
    assert "read_file" in _SIDE_EFFECT_IMPACT_BODY
    # DbC framing: which side is the defect.
    assert "callers" in lower and "defect" in lower


def test_side_effect_body_guard_names_the_one_narrow_exception_by_default() -> None:
    """With mutation analysis on, the no-prior-version guard must name its one
    exception rather than reading as a blanket, unconditional prohibition."""
    assert "never a prior version" in _SIDE_EFFECT_IMPACT_BODY
    assert "one narrow, explicit exception" in _SIDE_EFFECT_IMPACT_BODY
    assert "Replaced (pre-change) content" in _SIDE_EFFECT_IMPACT_BODY


def test_side_effect_body_no_mutation_omits_subcheck_and_stays_absolute() -> None:
    """The disabled (mutation-off) body variant must be byte-identical to the
    pre-mutation-analysis body: no sub-check, and an unconditional guard."""
    assert "mutation-vs-replaced-code" not in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION
    assert "Replaced (pre-change) content" not in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION
    assert (
        "**You are given the CURRENT content of the changed files only — never a "
        "prior version.** Do not guess, infer, or invent"
    ) in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION
    assert (
        'do not invent or assume a prior/"old" version of any function — you were not given one.'
        in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION.lower()
    )
    assert "EXCEPT" not in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION


def test_side_effect_body_no_mutation_still_documents_find_references() -> None:
    """Disabling the mutation sub-check must not remove the primary
    caller-impact check's own tool guidance."""
    assert "find_references" in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION
    assert "read_lines" in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION
    assert "read_function" in _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION


def test_build_side_effect_impact_reasoning_system_prompt_matches_toggle() -> None:
    assert (
        build_side_effect_impact_reasoning_system_prompt(mutation_on=True)
        == SIDE_EFFECT_IMPACT_REASONING_SYSTEM_PROMPT
    )
    off_prompt = build_side_effect_impact_reasoning_system_prompt(mutation_on=False)
    assert "mutation-vs-replaced-code" not in off_prompt
    assert off_prompt != SIDE_EFFECT_IMPACT_REASONING_SYSTEM_PROMPT


def test_merged_prompt_mutation_toggle_both_halves_on() -> None:
    """With both halves on, mutation_on=True reuses the precomputed merged
    constant; mutation_on=False must still carry both bodies but the
    no-mutation side-effect variant."""
    on_prompt = build_merged_architecture_side_effect_reasoning_system_prompt(
        arch_on=True, side_on=True, mutation_on=True
    )
    assert on_prompt == MERGED_ARCHITECTURE_SIDE_EFFECT_REASONING_SYSTEM_PROMPT

    off_prompt = build_merged_architecture_side_effect_reasoning_system_prompt(
        arch_on=True, side_on=True, mutation_on=False
    )
    assert _ARCHITECTURE_CONSISTENCY_BODY in off_prompt
    assert _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION in off_prompt
    assert "mutation-vs-replaced-code" not in off_prompt


def test_merged_prompt_mutation_toggle_side_only() -> None:
    on_prompt = build_merged_architecture_side_effect_reasoning_system_prompt(
        arch_on=False, side_on=True, mutation_on=True
    )
    assert _SIDE_EFFECT_IMPACT_BODY in on_prompt
    assert "mutation-vs-replaced-code" in on_prompt

    off_prompt = build_merged_architecture_side_effect_reasoning_system_prompt(
        arch_on=False, side_on=True, mutation_on=False
    )
    assert _SIDE_EFFECT_IMPACT_BODY_NO_MUTATION in off_prompt
    assert "mutation-vs-replaced-code" not in off_prompt


def test_merged_prompt_mutation_toggle_has_no_effect_when_side_off() -> None:
    """mutation_on is meaningless when the side-effect half itself is off."""
    arch_only_on = build_merged_architecture_side_effect_reasoning_system_prompt(
        arch_on=True, side_on=False, mutation_on=True
    )
    arch_only_off = build_merged_architecture_side_effect_reasoning_system_prompt(
        arch_on=True, side_on=False, mutation_on=False
    )
    assert arch_only_on == arch_only_off
