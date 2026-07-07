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
    calls: list[tuple] = []

    def _spy(llm, input_data, issues, repo_reader=None):
        calls.append((llm, input_data, issues))
        return issues

    monkeypatch.setattr(coord, "filter_false_positives", _spy)

    # Default: the filter runs once, with the call signature the coordinator
    # promises — the engine's LLM client, the CodeReviewInput, and the raw
    # issue list the chunk reviewer produced (asserting these guards against a
    # silent regression in how the coordinator invokes the filter).
    CodeReviewAgent(_IssueProbe()).run(CodeReviewInput(files={"a.py": "x = 1"}))
    assert len(calls) == 1
    spy_llm, spy_input, spy_issues = calls[0]
    assert isinstance(spy_input, CodeReviewInput)
    assert isinstance(spy_llm, _IssueProbe)
    assert isinstance(spy_issues, list) and spy_issues, "expected the raw chunk issues"

    # Skipped: the filter is bypassed entirely.
    CodeReviewAgent(_IssueProbe()).run(
        CodeReviewInput(files={"a.py": "x = 1"}, skip_false_positive_filter=True)
    )
    assert len(calls) == 1  # unchanged — no second call
