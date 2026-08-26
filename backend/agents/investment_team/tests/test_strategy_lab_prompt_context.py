"""Unit tests for the shared prompt-context helpers.

``render_prior_attempts`` and ``spec_prompt_fields`` are pure extractions of
blocks that were duplicated verbatim across the Strategy Lab agents. These
tests lock in both the extracted behaviour and, via characterization tests
that re-derive the original inline formulas independently, that the
extraction changed no rendered byte.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents._prompt_context import (
    _DEFAULT_KEEP_LAST_N,
    BoundedHistory,
    bound_history,
    render_prior_attempts,
    spec_prompt_fields,
)
from investment_team.strategy_lab.spec_dsl import format_rules_for_prompt, format_sizing_rule


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-prompt-context",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )


def _original_prior_attempts(prior_attempts: Optional[List[str]]) -> str:
    """The exact inline expression `render_prior_attempts` replaced."""
    return (
        "None yet."
        if not prior_attempts
        else "\n".join(f"  Round {i + 1}: {a}" for i, a in enumerate(prior_attempts))
    )


class _UnrecognizedSizing:
    """Neither `FixedFractionSizing`, `VolatilityTargetSizing`, nor
    `FixedNotionalSizing` — `format_sizing_rule` has no branch for it."""


# ---------------------------------------------------------------------------
# render_prior_attempts
# ---------------------------------------------------------------------------


def test_render_prior_attempts_none() -> None:
    assert render_prior_attempts(None) == "None yet."


def test_render_prior_attempts_empty_list() -> None:
    assert render_prior_attempts([]) == "None yet."


def test_render_prior_attempts_single_entry() -> None:
    assert render_prior_attempts(["fixed the guard"]) == "  Round 1: fixed the guard"


def test_render_prior_attempts_multiple_entries_are_one_indexed_in_order() -> None:
    result = render_prior_attempts(["first", "second", "third"])
    assert result == "  Round 1: first\n  Round 2: second\n  Round 3: third"


def test_render_prior_attempts_tolerates_non_string_elements() -> None:
    """Elements are only ever f-string-interpolated, never inspected."""
    result = render_prior_attempts([123, None, {"x": 1}])
    assert result == "  Round 1: 123\n  Round 2: None\n  Round 3: {'x': 1}"


@pytest.mark.parametrize(
    "prior_attempts",
    [None, [], ["one"], ["one", "two"], ["a", "b", "c", "d"]],
)
def test_render_prior_attempts_matches_original_inline_formula(
    prior_attempts: Optional[List[str]],
) -> None:
    """Characterization test: proves this is a byte-preserving extraction."""
    assert render_prior_attempts(prior_attempts) == _original_prior_attempts(prior_attempts)


# ---------------------------------------------------------------------------
# spec_prompt_fields
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = {
    "asset_class",
    "hypothesis",
    "signal_definition",
    "entry_rules",
    "exit_rules",
    "sizing_rules",
    "risk_limits",
}


def test_spec_prompt_fields_returns_exactly_seven_keys() -> None:
    assert set(spec_prompt_fields(_spec())) == _EXPECTED_KEYS


def test_spec_prompt_fields_defensive_false_matches_direct_access_formula() -> None:
    """Characterization test against the original non-defensive call sites'
    exact formula (e.g. `refinement.py`, `zero_trade_repair.py`)."""
    spec = _spec()
    expected = {
        "asset_class": spec.asset_class,
        "hypothesis": spec.hypothesis,
        "signal_definition": spec.signal_definition,
        "entry_rules": format_rules_for_prompt(spec.entry_rules),
        "exit_rules": format_rules_for_prompt(spec.exit_rules),
        "sizing_rules": format_sizing_rule(spec.sizing),
        "risk_limits": spec.risk_limits.model_dump_json(),
    }
    assert spec_prompt_fields(spec, defensive=False) == expected


def test_spec_prompt_fields_defensive_false_raises_on_missing_attribute() -> None:
    """Non-defensive mode adds no tolerance beyond what the original direct
    attribute access had."""
    with pytest.raises(AttributeError):
        spec_prompt_fields(SimpleNamespace(), defensive=False)


def test_spec_prompt_fields_defensive_true_on_none_returns_all_fallbacks() -> None:
    assert spec_prompt_fields(None, defensive=True) == {
        "asset_class": "?",
        "hypothesis": "?",
        "signal_definition": "?",
        "entry_rules": "",
        "exit_rules": "",
        "sizing_rules": "(none)",
        "risk_limits": "",
    }


def test_spec_prompt_fields_defensive_true_on_full_spec_matches_non_defensive() -> None:
    """Both branches converge on well-formed input."""
    spec = _spec()
    assert spec_prompt_fields(spec, defensive=True) == spec_prompt_fields(spec, defensive=False)


def test_spec_prompt_fields_defensive_true_sizing_none_renders_none_marker() -> None:
    fake_spec: Any = SimpleNamespace(sizing=None)
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["sizing_rules"] == "(none)"


def test_spec_prompt_fields_defensive_true_risk_limits_missing_renders_empty_string() -> None:
    """`risk_limits` attribute absent entirely -> the fallback getattr's
    default (`""`) is used."""
    fake_spec: Any = SimpleNamespace()
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["risk_limits"] == ""


def test_spec_prompt_fields_defensive_true_risk_limits_explicit_none_renders_none_string() -> None:
    """`risk_limits` attribute present but explicitly `None` -> the `hasattr`
    probe finds the attribute (returns `None`, not the probe's default), so
    the fallback getattr also finds it (returns `None`, not `""`) ->
    `str(None)`. This asymmetry vs. the "missing entirely" case above is
    inherited verbatim from the original `alignment.py` call site."""
    fake_spec: Any = SimpleNamespace(risk_limits=None)
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["risk_limits"] == "None"


def test_spec_prompt_fields_defensive_true_unrecognized_sizing_still_raises() -> None:
    """Defensive mode tolerates missing/None values, not malformed ones."""
    fake_spec: Any = SimpleNamespace(sizing=_UnrecognizedSizing())
    with pytest.raises(TypeError):
        spec_prompt_fields(fake_spec, defensive=True)


def test_spec_prompt_fields_defensive_true_none_rules_render_empty_string() -> None:
    fake_spec: Any = SimpleNamespace(entry_rules=None, exit_rules=None)
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["entry_rules"] == ""
    assert fields["exit_rules"] == ""


# ---------------------------------------------------------------------------
# bound_history
# ---------------------------------------------------------------------------


def test_bound_history_none_entries_is_empty_history() -> None:
    result = bound_history(None, keep_last_n=5)
    assert result == BoundedHistory(kept=[], summary="")


def test_bound_history_empty_list_is_empty_history() -> None:
    result = bound_history([], keep_last_n=5)
    assert result == BoundedHistory(kept=[], summary="")


def test_bound_history_empty_history_with_zero_keep() -> None:
    assert bound_history([], keep_last_n=0) == BoundedHistory(kept=[], summary="")


@pytest.mark.parametrize(
    "entries,keep_last_n",
    [
        (["a"], 1),
        (["a", "b"], 2),
        (["a", "b"], 5),
        (["a", "b", "c", "d"], 10),
    ],
)
def test_bound_history_short_history_unchanged_and_no_summary(
    entries: List[str], keep_last_n: int
) -> None:
    """len(entries) <= keep_last_n -> kept is the input verbatim, no summary."""
    result = bound_history(entries, keep_last_n)
    assert result.kept == entries
    assert result.summary == ""


def test_bound_history_short_history_kept_entries_are_the_original_objects() -> None:
    """Verbatim means the same objects, not copies or stringified entries."""
    a, b = object(), object()
    result = bound_history([a, b], keep_last_n=5)
    assert result.kept[0] is a
    assert result.kept[1] is b


def test_bound_history_exact_boundary_len_equals_keep_last_n() -> None:
    entries = ["a", "b", "c"]
    result = bound_history(entries, keep_last_n=3)
    assert result.kept == entries
    assert result.summary == ""


def test_bound_history_over_history_keeps_last_n_verbatim_in_order() -> None:
    entries = [f"entry-{i}" for i in range(10)]
    result = bound_history(entries, keep_last_n=3)
    assert result.kept == ["entry-7", "entry-8", "entry-9"]


def test_bound_history_over_history_kept_entries_are_the_original_objects() -> None:
    a, b, c, d = object(), object(), object(), object()
    result = bound_history([a, b, c, d], keep_last_n=2)
    assert result.kept[0] is c
    assert result.kept[1] is d


def test_bound_history_over_history_summary_mentions_dropped_count() -> None:
    entries = [f"entry-{i}" for i in range(10)]
    result = bound_history(entries, keep_last_n=3)
    assert "7 earlier round(s)" in result.summary


def test_bound_history_over_history_summary_numbers_dropped_rounds_from_one() -> None:
    entries = ["first", "second", "third", "fourth"]
    result = bound_history(entries, keep_last_n=1)
    assert "Round 1: first" in result.summary
    assert "Round 2: second" in result.summary
    assert "Round 3: third" in result.summary
    assert "fourth" not in result.summary


def test_bound_history_keep_last_n_zero_drops_everything() -> None:
    entries = ["a", "b", "c"]
    result = bound_history(entries, keep_last_n=0)
    assert result.kept == []
    assert "3 earlier round(s)" in result.summary


def test_bound_history_summary_is_length_bounded_regardless_of_round_count() -> None:
    """The rolling summary stays capped even with hundreds of dropped rounds,
    which is what makes it 'rolling' rather than merely per-item-truncated."""
    entries = [f"a very long prior-round entry description number {i}" * 3 for i in range(500)]
    result = bound_history(entries, keep_last_n=2)
    assert len(result.kept) == 2
    assert len(result.summary) <= 240  # _SUMMARY_MAX_CHARS, ellipsis included in the cap


def test_bound_history_summary_single_line_no_embedded_newlines() -> None:
    entries = ["line one\nline two", "b", "c"]
    result = bound_history(entries, keep_last_n=1)
    assert "\n" not in result.summary


def test_bound_history_summary_strips_carriage_returns_too() -> None:
    """CRLF and bare CR are also line separators, not just bare LF."""
    entries = ["line one\r\nline two", "line three\rline four", "b"]
    result = bound_history(entries, keep_last_n=1)
    assert "\r" not in result.summary
    assert "\n" not in result.summary
    assert "line one line two" in result.summary
    assert "line three line four" in result.summary


def test_bound_history_negative_keep_last_n_raises() -> None:
    with pytest.raises(AssertionError):
        bound_history(["a"], keep_last_n=-1)


# ---------------------------------------------------------------------------
# render_prior_attempts: bounded (bound_history wiring)
# ---------------------------------------------------------------------------


def test_render_prior_attempts_short_history_unaffected_by_bounding() -> None:
    """len(prior_attempts) <= keep_last_n -> identical to unbounded rendering,
    even with an explicit keep_last_n smaller than the module default."""
    prior_attempts = ["a", "b", "c"]
    assert render_prior_attempts(prior_attempts, keep_last_n=3) == (
        "  Round 1: a\n  Round 2: b\n  Round 3: c"
    )


def test_render_prior_attempts_over_cap_prepends_summary_line() -> None:
    prior_attempts = [f"attempt-{i}" for i in range(8)]
    result = render_prior_attempts(prior_attempts, keep_last_n=3)
    lines = result.split("\n")
    assert "earlier round(s) summarized" in lines[0]
    assert len(lines) == 1 + 3


def test_render_prior_attempts_over_cap_keeps_last_n_with_absolute_round_numbers() -> None:
    """Kept entries are numbered by their original position in the full
    list, not renumbered from 1 within the kept tail."""
    prior_attempts = [f"attempt-{i}" for i in range(8)]
    result = render_prior_attempts(prior_attempts, keep_last_n=3)
    assert "  Round 6: attempt-5" in result
    assert "  Round 7: attempt-6" in result
    assert "  Round 8: attempt-7" in result
    assert "Round 9" not in result


def test_render_prior_attempts_uses_module_default_keep_last_n() -> None:
    """Calling without keep_last_n bounds against `_DEFAULT_KEEP_LAST_N`."""
    prior_attempts = [f"attempt-{i}" for i in range(20)]
    result = render_prior_attempts(prior_attempts)
    lines = result.split("\n")
    assert "earlier round(s) summarized" in lines[0]
    assert len(lines) == 1 + _DEFAULT_KEEP_LAST_N  # summary line + N kept lines
    assert "  Round 20: attempt-19" in result


def test_render_prior_attempts_output_size_bounded_regardless_of_round_count() -> None:
    """Benchmark-style test: rendered length stays roughly constant as round
    count grows, instead of growing linearly with input size."""
    small = render_prior_attempts([f"attempt-{i}" for i in range(10)], keep_last_n=5)
    large = render_prior_attempts([f"attempt-{i}" for i in range(500)], keep_last_n=5)
    assert len(large) < len(small) * 3
