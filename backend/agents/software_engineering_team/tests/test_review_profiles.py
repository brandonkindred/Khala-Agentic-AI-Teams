"""Tests for the shared review-engine profiles.

Mirrors the Part-2 ``test_security_service`` style: an equivalence guard locking
the default profile to the legacy prompt, per-profile content checks, the shared
output contract, and a probe that the engine threads the selected profile down to
the chunk reviewer's system prompt.
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Equivalence guard: the default profile reproduces today's reviewer prompt.
# ---------------------------------------------------------------------------


def test_code_review_profile_is_byte_identical_to_legacy_prompt() -> None:
    """The default CODE_REVIEW profile reproduces the legacy prompt byte-for-byte."""
    assert build_review_system_prompt(ReviewProfile.CODE_REVIEW) == CODE_REVIEW_PROMPT


def test_shared_skeleton_pieces_are_slices_of_legacy_prompt() -> None:
    """The shared skeleton pieces are exact substrings of the canonical prompt,
    proving the transcription is exact and pinning the shared contract."""
    assert _SHARED_ROLE_AND_SETTLED in CODE_REVIEW_PROMPT
    assert _SHARED_OUTPUT_SECTION in CODE_REVIEW_PROMPT


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


def test_builder_accepts_string_value() -> None:
    """build_review_system_prompt accepts a profile's string value as well as the enum."""
    assert build_review_system_prompt("spec_conformance") == build_review_system_prompt(
        ReviewProfile.SPEC_CONFORMANCE
    )


def test_builder_rejects_unknown_profile() -> None:
    """An unknown profile value raises ValueError."""
    with pytest.raises(ValueError):
        build_review_system_prompt("not_a_profile")


def test_registry_exhausts_all_profiles() -> None:
    """The profile registry has an entry for every ReviewProfile member."""
    assert set(REVIEW_PROFILES) == set(ReviewProfile)


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
        return {"approved": True, "issues": [], "summary": "ok"}


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
    CodeReviewAgent(probe).run(CodeReviewInput(code="def f():\n    return 1", profile=profile))
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
        }


def test_skip_false_positive_filter_bypasses_verifier(monkeypatch) -> None:
    """skip_false_positive_filter=True bypasses the whole-codebase verifier call;
    the default runs it once."""
    import code_review_agent.coordinator as coord

    called = {"n": 0}

    def _spy(llm, input_data, issues):
        called["n"] += 1
        return issues

    monkeypatch.setattr(coord, "filter_false_positives", _spy)

    # Default: the filter runs.
    CodeReviewAgent(_IssueProbe()).run(CodeReviewInput(files={"a.py": "x = 1"}))
    assert called["n"] == 1

    # Skipped: the filter is bypassed entirely.
    CodeReviewAgent(_IssueProbe()).run(
        CodeReviewInput(files={"a.py": "x = 1"}, skip_false_positive_filter=True)
    )
    assert called["n"] == 1  # unchanged — no second call
