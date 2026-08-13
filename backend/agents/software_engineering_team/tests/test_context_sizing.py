"""Tests for context_sizing's combined-prompt budget guarantees.

Section helpers are sized independently, but agents combine several in one
prompt (e.g. BackendAgent._plan_task includes spec content AND existing
code). Each section is therefore ceilinged at a fraction of the model
context so the dominant two-big-section prompts always fit — at 1M-token
contexts the previously uncapped helpers each allowed ~3.4M chars and the
combination overflowed the window.

Submission-pass / merged-pass character budgets were removed; those paths
no longer use ``context_sizing`` for prompt packing.
"""

from __future__ import annotations

import pytest

from software_engineering_team.shared.context_sizing import (
    CHARS_PER_TOKEN,
    CODE_REVIEW_ABS_CHUNK_CHARS,
    CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS,
    CODE_REVIEW_EXISTING_ABS_CHARS,
    CODE_REVIEW_SPEC_EXCERPT_ABS_CHARS,
    compute_code_review_arch_overview_chars,
    compute_code_review_chunk_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_spec_excerpt_chars,
    compute_existing_code_chars,
    compute_max_chunk_chars,
    compute_spec_content_chars,
    parse_env_int,
)


class _StubLLM:
    def __init__(self, ctx: int) -> None:
        self._ctx = ctx

    def get_max_context_tokens(self) -> int:
        return self._ctx


def test_single_section_never_exceeds_fraction_of_context() -> None:
    llm = _StubLLM(1000000)
    ceiling_chars = int(0.4 * 1000000 * CHARS_PER_TOKEN)
    assert compute_existing_code_chars(llm) <= ceiling_chars
    assert compute_spec_content_chars(llm) <= ceiling_chars


def test_two_big_sections_plus_reserves_fit_the_window() -> None:
    """The BackendAgent._plan_task shape: spec content + existing code in one
    prompt, plus prompt/response reserves, must stay inside the context."""
    llm = _StubLLM(1000000)
    spec_tokens = compute_spec_content_chars(llm) / CHARS_PER_TOKEN
    code_tokens = compute_existing_code_chars(llm) / CHARS_PER_TOKEN
    reserves = 12000 + 8192  # the helpers' own prompt/response reservations
    assert spec_tokens + code_tokens + reserves <= 1000000


def test_small_model_floors_unchanged() -> None:
    llm = _StubLLM(16384)
    assert compute_existing_code_chars(llm) == 20000  # min_chars floor
    assert compute_spec_content_chars(llm) == 15000  # min_chars floor


def test_max_fraction_is_overridable_per_call() -> None:
    full = compute_max_chunk_chars(1000000, max_fraction_of_context=1.0)
    capped = compute_max_chunk_chars(1000000)
    assert capped < full


# ---------------------------------------------------------------------------
# Code-review absolute caps (map-reduce review)
# ---------------------------------------------------------------------------


def test_map_chunk_is_absolutely_capped_at_large_context() -> None:
    """At 1M-token context the context-derived chunk size is ~1.4M chars; the
    cap is applied inside ``compute_code_review_chunk_chars`` itself so no
    caller can ever obtain the unbounded size."""
    llm = _StubLLM(1000000)
    assert compute_code_review_chunk_chars(llm) == CODE_REVIEW_ABS_CHUNK_CHARS == 80_000
    assert compute_code_review_map_chunk_chars(llm) == CODE_REVIEW_ABS_CHUNK_CHARS


def test_map_chunk_uses_context_derived_size_for_small_models() -> None:
    llm = _StubLLM(16384)
    derived = compute_code_review_chunk_chars(llm)
    assert derived < CODE_REVIEW_ABS_CHUNK_CHARS
    assert compute_code_review_map_chunk_chars(llm) == derived


def test_map_chunk_cap_is_env_overridable(monkeypatch) -> None:
    llm = _StubLLM(1000000)
    monkeypatch.setenv("CODE_REVIEW_MAP_CHUNK_CHARS", "120000")
    assert compute_code_review_chunk_chars(llm) == 120_000
    monkeypatch.setenv("CODE_REVIEW_MAP_CHUNK_CHARS", "garbage")
    assert compute_code_review_chunk_chars(llm) == CODE_REVIEW_ABS_CHUNK_CHARS
    monkeypatch.setenv("CODE_REVIEW_MAP_CHUNK_CHARS", "5")
    assert compute_code_review_chunk_chars(llm) == 10_000  # clamped to the floor


def test_parse_env_int_defensive_parsing(monkeypatch) -> None:
    monkeypatch.delenv("SOME_KNOB", raising=False)
    assert parse_env_int("SOME_KNOB", 7, 1) == 7
    monkeypatch.setenv("SOME_KNOB", " 42 ")
    assert parse_env_int("SOME_KNOB", 7, 1) == 42
    monkeypatch.setenv("SOME_KNOB", "nope")
    assert parse_env_int("SOME_KNOB", 7, 1) == 7
    monkeypatch.setenv("SOME_KNOB", "-3")
    assert parse_env_int("SOME_KNOB", 7, 1) == 1


def test_parse_env_int_rejects_default_below_floor() -> None:
    """Precondition (default >= floor) is enforced with an explicit ValueError
    so the check survives ``python -O`` (assert would be stripped)."""
    with pytest.raises(ValueError):
        parse_env_int("SOME_KNOB", 1, 5)


def test_code_review_excerpts_are_absolutely_capped_at_large_context() -> None:
    """The spec/arch/existing excerpts repeat in every map call; uncapped they
    scale to hundreds of K chars at 1M context."""
    llm = _StubLLM(1000000)
    assert compute_code_review_spec_excerpt_chars(llm) == CODE_REVIEW_SPEC_EXCERPT_ABS_CHARS
    assert compute_code_review_arch_overview_chars(llm) == CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS
    assert compute_code_review_existing_codebase_chars(llm) == CODE_REVIEW_EXISTING_ABS_CHARS


def test_code_review_excerpt_floors_unchanged_for_small_models() -> None:
    llm = _StubLLM(16384)
    assert compute_code_review_spec_excerpt_chars(llm) == 8_000
    assert compute_code_review_arch_overview_chars(llm) == 2_000
    assert compute_code_review_existing_codebase_chars(llm) == 4_000
