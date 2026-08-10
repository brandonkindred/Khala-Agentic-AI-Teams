"""Tests for context_sizing's combined-prompt budget guarantees.

Section helpers are sized independently, but agents combine several in one
prompt (e.g. BackendAgent._plan_task includes spec content AND existing
code). Each section is therefore ceilinged at a fraction of the model
context so the dominant two-big-section prompts always fit — at 1M-token
contexts the previously uncapped helpers each allowed ~3.4M chars and the
combination overflowed the window.
"""

from __future__ import annotations

import pytest

from software_engineering_team.shared.context_sizing import (
    CHARS_PER_TOKEN,
    CODE_REVIEW_ABS_CHUNK_CHARS,
    CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS,
    CODE_REVIEW_EXISTING_ABS_CHARS,
    CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS,
    CODE_REVIEW_SPEC_EXCERPT_ABS_CHARS,
    code_review_tail_pass_chunk_chars_cap,
    compute_code_review_arch_overview_chars,
    compute_code_review_chunk_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_merged_pass_budgets,
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


def test_tail_pass_chunk_cap_defaults_to_map_chunk_default() -> None:
    """Unset, the tail-pass cap must equal today's shared value so batching
    behavior for typical-size submissions is unchanged (AC3 of #4376)."""
    assert code_review_tail_pass_chunk_chars_cap() == CODE_REVIEW_ABS_CHUNK_CHARS


def test_tail_pass_chunk_cap_is_env_overridable(monkeypatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_TAIL_PASS_CHUNK_CHARS", "120000")
    assert code_review_tail_pass_chunk_chars_cap() == 120_000
    monkeypatch.setenv("CODE_REVIEW_TAIL_PASS_CHUNK_CHARS", "garbage")
    assert code_review_tail_pass_chunk_chars_cap() == CODE_REVIEW_ABS_CHUNK_CHARS
    monkeypatch.setenv("CODE_REVIEW_TAIL_PASS_CHUNK_CHARS", "5")
    assert code_review_tail_pass_chunk_chars_cap() == 10_000  # clamped to the floor


def test_tail_pass_chunk_cap_is_decoupled_from_map_chunk_cap(monkeypatch) -> None:
    """Setting one knob must never move the other's effective cap."""
    monkeypatch.setenv("CODE_REVIEW_MAP_CHUNK_CHARS", "150000")
    assert code_review_tail_pass_chunk_chars_cap() == CODE_REVIEW_ABS_CHUNK_CHARS
    monkeypatch.delenv("CODE_REVIEW_MAP_CHUNK_CHARS", raising=False)

    monkeypatch.setenv("CODE_REVIEW_TAIL_PASS_CHUNK_CHARS", "150000")
    llm = _StubLLM(1000000)
    assert compute_code_review_chunk_chars(llm) == CODE_REVIEW_ABS_CHUNK_CHARS


def test_merged_pass_budgets_code_cap_uses_tail_pass_knob(monkeypatch) -> None:
    """The merged pass's ``max_inline_code_chars`` must track
    ``CODE_REVIEW_TAIL_PASS_CHUNK_CHARS`` (not ``CODE_REVIEW_MAP_CHUNK_CHARS``)."""
    llm = _StubLLM(1000000)
    monkeypatch.setenv("CODE_REVIEW_TAIL_PASS_CHUNK_CHARS", "30000")
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        finding_array_count=1,
    )
    assert budgets is not None
    assert budgets.max_inline_code_chars == 30_000
    monkeypatch.delenv("CODE_REVIEW_TAIL_PASS_CHUNK_CHARS", raising=False)

    monkeypatch.setenv("CODE_REVIEW_MAP_CHUNK_CHARS", "30000")
    unaffected = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        finding_array_count=1,
    )
    assert unaffected is not None
    assert unaffected.max_inline_code_chars == CODE_REVIEW_ABS_CHUNK_CHARS


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


def test_merged_pass_budgets_prefer_full_arch_and_shrink_code() -> None:
    """A large architecture body must reduce the changed-file inline budget
    relative to the map-call allowance, instead of reusing that allowance
    unchanged while still inlining the full document."""
    llm = _StubLLM(40_000)
    map_budget = compute_code_review_map_chunk_chars(llm)
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=40_000,
        system_prompt_chars=14_000,
    )
    assert budgets is not None
    assert budgets.max_architecture_chars == 40_000
    assert budgets.max_inline_code_chars < map_budget
    assert budgets.max_inline_code_chars <= CODE_REVIEW_ABS_CHUNK_CHARS
    assert budgets.reserved_response_tokens == CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS


def test_merged_pass_budgets_cap_architecture_when_it_cannot_fit() -> None:
    """On a small-context model, an oversized architecture document is capped
    so the merged call stays inside the window; code inlining yields to it."""
    llm = _StubLLM(16_384)
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=100_000,
        system_prompt_chars=14_000,
        tool_schema_chars=0,
        tool_transcript_chars=0,
    )
    assert budgets is not None
    assert budgets.max_architecture_chars < 100_000
    assert budgets.max_architecture_chars > 0
    assert budgets.max_inline_code_chars == 0


def test_merged_pass_budgets_return_none_when_fixed_reserves_exceed_context() -> None:
    """When system prompt + scaffolding leave no usable response room, skip
    rather than inventing a positive inline allowance."""
    llm = _StubLLM(8_192)
    # Isolate prompt/response math from tool schema/transcript reserves.
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=14_000,
        tool_schema_chars=0,
        tool_transcript_chars=0,
        finding_array_count=2,
    )
    impossible = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=50_000,
        tool_schema_chars=0,
        tool_transcript_chars=0,
    )
    assert impossible is None
    assert budgets is not None
    assert budgets.max_architecture_chars == 0
    assert budgets.max_inline_code_chars == 0
    assert budgets.reserved_response_tokens < CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS
    assert budgets.reserved_response_tokens >= 1024
    # Default tool schema + transcript reserves make this 8K window unusable
    # for a ~14K-char system prompt (no room for response + tool evidence).
    assert (
        compute_code_review_merged_pass_budgets(
            llm,
            architecture_chars=0,
            system_prompt_chars=14_000,
            finding_array_count=2,
        )
        is None
    )


def test_merged_pass_budgets_bound_manifest_within_content_room() -> None:
    llm = _StubLLM(40_000)
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=1_000,
        system_prompt_chars=5_000,
        manifest_chars=200_000,
    )
    assert budgets is not None
    assert budgets.max_architecture_chars == 1_000
    assert budgets.max_manifest_chars < 200_000
    assert (
        budgets.max_manifest_chars + budgets.max_architecture_chars + budgets.max_inline_code_chars
    ) <= int(
        (40_000 - int((5_000 + 1_500) / CHARS_PER_TOKEN) - budgets.reserved_response_tokens)
        * CHARS_PER_TOKEN
    ) + 1  # float rounding


def test_merged_pass_budgets_prefer_architecture_over_recoverable_manifest() -> None:
    """Architecture text has no retrieval tool; a huge changed-file manifest
    must not zero out the document budget — the manifest is recoverable via
    list_changed_files()."""
    llm = _StubLLM(16_384)
    budgets = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=8_000,
        system_prompt_chars=5_000,
        manifest_chars=200_000,
        finding_array_count=2,
        tool_schema_chars=0,
        tool_transcript_chars=0,
    )
    assert budgets is not None
    assert budgets.max_architecture_chars == 8_000
    assert budgets.max_manifest_chars < 200_000


def test_merged_pass_budgets_single_half_uses_smaller_response_reserve() -> None:
    # Context tight enough that the response-reserve difference still shows up
    # in content room (large contexts both hit the absolute map-chunk cap).
    llm = _StubLLM(20_000)
    both = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        finding_array_count=2,
        tool_schema_chars=0,
        tool_transcript_chars=0,
    )
    one = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        finding_array_count=1,
        tool_schema_chars=0,
        tool_transcript_chars=0,
    )
    assert both is not None and one is not None
    assert both.reserved_response_tokens == CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS
    assert one.reserved_response_tokens == 4096
    assert one.max_inline_code_chars > both.max_inline_code_chars


def test_merged_pass_response_reserve_is_dual_array_floor() -> None:
    assert CODE_REVIEW_MERGED_PASS_RESPONSE_TOKENS == 8192


def test_merged_pass_budgets_reserve_tool_schemas() -> None:
    """Tool schemas consume context outside the prompt bodies; without a
    reserve, an 8K model can allocate all remaining room to the response and
    still overflow once schemas are attached."""
    llm = _StubLLM(8_192)
    without_tools = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=14_000,
        tool_schema_chars=0,
        tool_transcript_chars=0,
        finding_array_count=2,
    )
    with_tools = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=14_000,
        tool_transcript_chars=0,
        finding_array_count=2,
    )
    assert without_tools is not None
    assert with_tools is not None
    assert with_tools.reserved_response_tokens <= without_tools.reserved_response_tokens
    # Fixed reserves that cannot leave a usable response room skip the call.
    impossible = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=14_000,
        tool_schema_chars=50_000,
        tool_transcript_chars=0,
        finding_array_count=2,
    )
    assert impossible is None


def test_merged_pass_budgets_reserve_tool_transcript() -> None:
    """Tool-call results append mid-turn; without transcript headroom the
    initial content can consume every token between prompt and response."""
    # Context tight enough that the map-chunk absolute cap does not hide the
    # transcript room difference.
    llm = _StubLLM(25_000)
    without = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        tool_schema_chars=0,
        tool_transcript_chars=0,
        finding_array_count=2,
    )
    with_transcript = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        tool_schema_chars=0,
        finding_array_count=2,
    )
    assert without is not None and with_transcript is not None
    assert (
        with_transcript.max_inline_code_chars + with_transcript.max_manifest_chars
        < without.max_inline_code_chars + without.max_manifest_chars
    )
    # Transcript + min response that cannot fit → skip.
    tiny = compute_code_review_merged_pass_budgets(
        llm,
        architecture_chars=0,
        system_prompt_chars=5_000,
        tool_schema_chars=0,
        tool_transcript_chars=200_000,
        finding_array_count=2,
    )
    assert tiny is None
