"""Tests for the shared review-engine profiles.

Mirrors the Part-2 ``test_security_service`` style: an equivalence guard locking
the default profile to the legacy prompt, per-profile content checks, the shared
output contract, and a probe that the engine threads the selected profile down to
the chunk reviewer's system prompt.
"""

from __future__ import annotations

import code_review_agent.coordinator as coord
import pytest
from code_review_agent import CodeReviewAgent, CodeReviewInput, ReviewProfile
from code_review_agent.profiles import (
    _SHARED_OUTPUT_SECTION,
    _SHARED_ROLE_AND_SETTLED,
    REVIEW_PROFILES,
    build_review_system_prompt,
)
from code_review_agent.prompts import CODE_REVIEW_PROMPT

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.prompts import REQUIREMENT_CITATION_GUARDRAIL

# ---------------------------------------------------------------------------
# Equivalence guard: CODE_REVIEW_PROMPT is derived from the profile builder.
# ---------------------------------------------------------------------------


def test_code_review_profile_is_byte_identical_to_legacy_prompt() -> None:
    """CODE_REVIEW_PROMPT is an alias of build_review_system_prompt(CODE_REVIEW)."""
    assert build_review_system_prompt(ReviewProfile.CODE_REVIEW) == CODE_REVIEW_PROMPT


def test_shared_skeleton_pieces_are_slices_of_legacy_prompt() -> None:
    """Shared skeleton pieces remain substrings of the derived CODE_REVIEW prompt."""
    assert _SHARED_ROLE_AND_SETTLED in CODE_REVIEW_PROMPT
    assert _SHARED_OUTPUT_SECTION in CODE_REVIEW_PROMPT


def test_requirement_citation_guardrail_in_spec_flavored_sections_only() -> None:
    """Guardrail sits under the Ticket/Spec Fit item, not Style."""
    code_review = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert REQUIREMENT_CITATION_GUARDRAIL in code_review
    # Search after the Ticket/Spec Fit checklist item — Naming also appears earlier
    # in REVIEW_PRIORITY_FRAMEWORK.
    i_ticket = code_review.index("7. **Ticket/Spec Fit**")
    i_guard = code_review.index(REQUIREMENT_CITATION_GUARDRAIL, i_ticket)
    i_style = code_review.index("8. **Style**", i_ticket)
    assert i_ticket < i_guard < i_style

    spec_conf = build_review_system_prompt(ReviewProfile.SPEC_CONFORMANCE)
    assert REQUIREMENT_CITATION_GUARDRAIL in spec_conf
    assert spec_conf.count(REQUIREMENT_CITATION_GUARDRAIL) >= 2

    senior = build_review_system_prompt(ReviewProfile.SENIOR_ARCHITECTURE)
    assert REQUIREMENT_CITATION_GUARDRAIL in senior
    i_cov = senior.index("2. **Spec Coverage**")
    i_g = senior.index(REQUIREMENT_CITATION_GUARDRAIL, i_cov)
    i_risk = senior.index("3. **Maintainability & Risk**", i_cov)
    assert i_cov < i_g < i_risk


def test_requirement_citation_guardrail_absent_from_non_spec_profiles() -> None:
    """ACCEPTANCE (already stricter) and DevOps maintainability stay unchanged."""
    assert REQUIREMENT_CITATION_GUARDRAIL not in build_review_system_prompt(
        ReviewProfile.DEVOPS_MAINTAINABILITY
    )
    assert REQUIREMENT_CITATION_GUARDRAIL not in build_review_system_prompt(
        ReviewProfile.ACCEPTANCE
    )


# ---------------------------------------------------------------------------
# Profile registry + builder behavior.
# ---------------------------------------------------------------------------


def test_all_profiles_share_the_json_output_contract() -> None:
    """Every profile advertises the same JSON output schema so the coordinator
    parser and the dummy stubs stay profile-agnostic."""
    for profile in ReviewProfile:
        prompt = build_review_system_prompt(profile)
        assert '"approved": boolean' in prompt
        assert '"severity": "critical" | "high" | "medium" | "low" | "info"' in prompt
        assert '"suggestion"' in prompt
        # Shared standards skeleton present for every profile.
        assert _SHARED_OUTPUT_SECTION in prompt


def test_summary_guidance_enforces_brevity_and_no_praise() -> None:
    """The shared output contract steers the summary toward a brief, high-level
    overview and forbids restating the PR or praising it when issues exist."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert "Do NOT restate what the PR does or is meant to accomplish" in prompt
    assert "common theme across them" in prompt
    # Spec-compliance notes are gaps-only and empty when there are none.
    assert 'return an empty string "" — do not write reassuring "meets the spec" prose' in prompt


def test_thoroughness_requirements_are_surface_scoped_not_whole_codebase() -> None:
    """Regression guard: the shared THOROUGHNESS REQUIREMENTS block scopes the
    reviewer's obligation to the code it was given to review, not every file
    the wider codebase happens to contain -- and no longer carries the old
    "review EVERY function/class in EVERY file" mandate that predated the
    diff-first rewrite."""
    assert (
        "Your thoroughness obligation is everything in the code you were given to review"
        in _SHARED_OUTPUT_SECTION
    )
    assert '"Code to review" input' in _SHARED_OUTPUT_SECTION
    assert (
        "Do NOT extend that obligation to code shown to you only as background"
        in _SHARED_OUTPUT_SECTION
    )
    # The retired whole-codebase thoroughness mandate must not reappear.
    assert "EVERY file" not in _SHARED_OUTPUT_SECTION
    assert "EVERY function, method, and class" not in _SHARED_OUTPUT_SECTION
    assert "review every function" not in _SHARED_OUTPUT_SECTION.lower()


def test_code_review_criteria_covers_eight_change_focused_headers() -> None:
    """The default CODE_REVIEW profile's checklist covers all eight change-focused
    criteria the diff-first review goal requires."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert "1. **Correctness**" in prompt
    assert "2. **Contracts**" in prompt
    assert "3. **Caller Side Effects**" in prompt
    assert "4. **Architecture**" in prompt
    assert "5. **Best Practices**" in prompt
    assert "6. **New Issues**" in prompt
    assert "7. **Ticket/Spec Fit**" in prompt
    assert "8. **Style**" in prompt


def test_new_criteria_severity_guidance_caps_default_severity() -> None:
    """The Architecture and New Issues criteria's severity guidance defaults to
    medium/low/info and names the narrow condition for escalation, so the checks
    add feedback without flooding the approval gate with noise."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert "would actually break integration" in prompt
    assert "not merely because a cleaner alternative exists" in prompt
    assert "not for a design preference alone" in prompt


def test_code_review_contracts_criterion_covers_dbc_and_documentation_accuracy() -> None:
    """Contracts consolidates Design by Contract framing with the docstring-accuracy
    guidance that used to live under the old Documentation item."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert "Preconditions: conditions the function requires" in prompt
    assert "Postconditions: what the function guarantees" in prompt
    assert "Invariants: properties that hold before and after" in prompt
    assert (
        "A docstring or comment that claims behavior the implementation does not provide" in prompt
    )


def test_code_review_style_criterion_keeps_no_fixed_word_limit_guidance() -> None:
    """The naming guidance's hard-won 'no fixed word limit' framing survives
    consolidation into the Style criterion."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert "There is NO fixed word limit" in prompt


def test_code_review_new_issues_criterion_reinforces_pre_existing_semantics() -> None:
    """New Issues explicitly ties its scope to the pre_existing JSON field so
    reviewers separate diff-introduced defects from pre-existing ones."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert '"pre_existing" field to make that distinction' in prompt


def test_output_contract_accepts_new_categories() -> None:
    """The JSON output contract's category enum accepts the three new axes this
    expansion adds (architecture, refactor, maintainability)."""
    prompt = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert '"architecture" | "refactor" | "maintainability"' in prompt


@pytest.mark.parametrize(
    ("profile", "anchor"),
    [
        (ReviewProfile.SPEC_CONFORMANCE, "SPEC CONFORMANCE"),
        (ReviewProfile.ACCEPTANCE, "ACCEPTANCE VERIFICATION"),
        (ReviewProfile.SENIOR_ARCHITECTURE, "SENIOR ARCHITECT"),
        (ReviewProfile.DEVOPS_MAINTAINABILITY, "DEVOPS MAINTAINABILITY"),
    ],
)
def test_each_profile_has_its_own_criteria(profile: ReviewProfile, anchor: str) -> None:
    """Each non-default profile's prompt carries its own criteria anchor."""
    assert anchor in build_review_system_prompt(profile)


def test_acceptance_profile_instructs_criterion_tagging() -> None:
    """The acceptance profile instructs one issue per unmet criterion, carries the
    criterion in the description prefix (delimiter " :: "), and keeps category as a
    valid output-contract enum value."""
    prompt = build_review_system_prompt(ReviewProfile.ACCEPTANCE)
    assert "EXACTLY ONE issue for each criterion that is NOT fully satisfied" in prompt
    assert "VERBATIM acceptance-criterion text" in prompt
    assert " :: " in prompt
    assert '"category" to "spec-compliance"' in prompt


@pytest.mark.parametrize("profile", list(ReviewProfile))
def test_builder_accepts_string_value(profile: ReviewProfile) -> None:
    """build_review_system_prompt accepts every profile's string value, and the
    coercion yields the same prompt as passing the enum member itself."""
    assert build_review_system_prompt(profile.value) == build_review_system_prompt(profile)


@pytest.mark.parametrize("bad", ["not_a_profile", None, 42, object()])
def test_builder_rejects_unknown_profile(bad: object) -> None:
    """An unknown profile value — a bad string, None, an int, or an arbitrary
    object — raises ValueError consistently (str-Enum coercion)."""
    with pytest.raises(ValueError):
        build_review_system_prompt(bad)


def test_registry_exhausts_all_profiles() -> None:
    """The profile registry has an entry for every ReviewProfile member."""
    assert set(REVIEW_PROFILES) == set(ReviewProfile)


def test_criteria_block_ends_without_trailing_newline() -> None:
    """Every profile's criteria_block ends without a trailing newline (the
    _ProfileSpec invariant), so it composes cleanly into _SHARED_OUTPUT_SECTION
    without introducing extra blank lines."""
    for profile, spec in REVIEW_PROFILES.items():
        assert not spec.criteria_block.endswith("\n"), profile


# ---------------------------------------------------------------------------
# Threading: the engine routes the selected profile to the chunk reviewer.
# ---------------------------------------------------------------------------


class _SystemPromptProbe(DummyLLMClient):
    """Captures the ``system_prompt`` each chunk-review call receives."""

    def __init__(self):
        super().__init__()
        self.system_prompts: list[str] = []

    def complete_json(self, prompt, *, system_prompt=None, **kwargs):
        self.system_prompts.append(system_prompt or "")
        return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}


@pytest.mark.parametrize(
    ("profile", "anchor"),
    [
        (ReviewProfile.CODE_REVIEW, "Senior Code Reviewer"),
        (ReviewProfile.SPEC_CONFORMANCE, "SPEC CONFORMANCE"),
        (ReviewProfile.DEVOPS_MAINTAINABILITY, "DEVOPS MAINTAINABILITY"),
    ],
)
def test_engine_threads_profile_to_chunk_reviewer(profile: ReviewProfile, anchor: str) -> None:
    """Running the engine with a profile routes that profile's system prompt to
    the chunk reviewer."""
    probe = _SystemPromptProbe()
    CodeReviewAgent(probe, force_in_process=True).run(
        CodeReviewInput(code="def f():\n    return 1", profile=profile)
    )
    assert probe.system_prompts, "expected at least one chunk-review call"
    assert any(anchor in sp for sp in probe.system_prompts)


def test_skip_false_positive_filter_field_default_off() -> None:
    """The skip_false_positive_filter input field defaults to False and is settable."""
    assert CodeReviewInput(code="x").skip_false_positive_filter is False
    assert CodeReviewInput(code="x", skip_false_positive_filter=True).skip_false_positive_filter


class _IssueProbe(DummyLLMClient):
    """Emits one engine finding on the chunk-review call."""

    def complete_json(self, prompt, **kwargs):
        return {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "logic",
                    "file_path": "a.py",
                    "description": "bug",
                    "suggestion": "fix",
                }
            ],
            "summary": "x",
            "spec_compliance_notes": "",
        }


def test_skip_false_positive_filter_bypasses_verifier(monkeypatch) -> None:
    """skip_false_positive_filter=True bypasses the whole-codebase verifier call;
    the default runs it once."""
    calls: list[tuple] = []

    def _spy(llm, input_data, issues, repo_reader=None, index=None):
        calls.append((llm, input_data, issues))
        return issues

    monkeypatch.setattr(coord, "filter_false_positives", _spy)

    # Default: the filter runs once, with the call signature the coordinator
    # promises — the engine's LLM client, the CodeReviewInput, and the raw
    # issue list the chunk reviewer produced (asserting these guards against a
    # silent regression in how the coordinator invokes the filter).
    CodeReviewAgent(_IssueProbe(), force_in_process=True).run(
        CodeReviewInput(files={"a.py": "x = 1"})
    )
    assert len(calls) == 1
    spy_llm, spy_input, spy_issues = calls[0]
    assert isinstance(spy_input, CodeReviewInput)
    assert isinstance(spy_llm, _IssueProbe)
    assert isinstance(spy_issues, list) and spy_issues, "expected the raw chunk issues"

    # Skipped: the filter is bypassed entirely.
    CodeReviewAgent(_IssueProbe(), force_in_process=True).run(
        CodeReviewInput(files={"a.py": "x = 1"}, skip_false_positive_filter=True)
    )
    assert len(calls) == 1  # unchanged — no second call


def test_skip_tail_passes_field_default_off() -> None:
    """The skip_tail_passes input field defaults to False and is settable."""
    assert CodeReviewInput(code="x").skip_tail_passes is False
    assert CodeReviewInput(code="x", skip_tail_passes=True).skip_tail_passes


def test_skip_tail_passes_bypasses_both_tail_passes(monkeypatch) -> None:
    """skip_tail_passes=True bypasses both the false-positive filter and the
    merged architecture/side-effect pass; the default runs both once."""
    filter_calls: list[tuple] = []
    merged_calls: list[tuple] = []

    def _filter_spy(llm, input_data, issues, repo_reader=None, index=None):
        filter_calls.append((llm, input_data, issues))
        return issues

    def _merged_spy(llm, input_data, repo_reader=None, index=None):
        merged_calls.append((llm, input_data))
        return ([], [])

    monkeypatch.setattr(coord, "filter_false_positives", _filter_spy)
    monkeypatch.setattr(coord, "find_architecture_and_side_effect_issues", _merged_spy)

    # Default: both tail passes run once.
    CodeReviewAgent(_IssueProbe(), force_in_process=True).run(
        CodeReviewInput(files={"a.py": "x = 1"})
    )
    assert len(filter_calls) == 1
    assert len(merged_calls) == 1

    # Skipped: neither tail pass runs.
    CodeReviewAgent(_IssueProbe(), force_in_process=True).run(
        CodeReviewInput(files={"a.py": "x = 1"}, skip_tail_passes=True)
    )
    assert len(filter_calls) == 1  # unchanged — no second call
    assert len(merged_calls) == 1  # unchanged — no second call
