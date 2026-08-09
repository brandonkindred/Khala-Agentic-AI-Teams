"""Unit tests for ``StrategyLabBudgetConfig`` (env resolution + validation)."""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.budget_config import StrategyLabBudgetConfig

# Every env var StrategyLabBudgetConfig.from_env() reads, including the
# generic LLM_* fallbacks it cascades through — cleared before each "defaults"
# assertion so ambient environment/CI config cannot leak into the expected values.
_ALL_ENV_VARS = (
    "STRATEGY_LAB_ALIGNMENT_RETRIES",
    "STRATEGY_LAB_LLM_MAX_RETRIES",
    "LLM_MAX_RETRIES",
    "STRATEGY_LAB_LLM_TIMEOUT",
    "LLM_TIMEOUT",
    "STRATEGY_LAB_LLM_BACKOFF_BASE",
    "LLM_BACKOFF_BASE",
    "STRATEGY_LAB_LLM_BACKOFF_MAX",
    "LLM_BACKOFF_MAX",
    "STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL",
    "LLM_RATE_LIMIT_BACKOFF_INITIAL",
    "STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX",
    "LLM_RATE_LIMIT_BACKOFF_MAX",
    "LLM_RATE_LIMIT_MAX_RETRIES",
    "STRATEGY_LAB_LLM_TOTAL_BUDGET",
    "STRATEGY_LAB_DESIGN_REVIEW_ROUNDS",
    "STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS",
    "STRATEGY_LAB_DESIGN_PARSE_RETRIES",
    "STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS",
    "STRATEGY_LAB_DESIGN_MAX_LLM_CALLS",
    "STRATEGY_LAB_REFINEMENT_PARSE_RETRIES",
    "STRATEGY_LAB_REFINEMENT_STALL_ROUNDS",
    "STRATEGY_LAB_MAX_CODE_REFINEMENT_ROUNDS",
    "STRATEGY_LAB_CODE_CONFORMANCE_RETRIES",
    "STRATEGY_LAB_MAX_ALIGNMENT_ROUNDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# from_env() defaults
# ---------------------------------------------------------------------------


def test_from_env_defaults_match_dataclass_defaults() -> None:
    """With nothing set, ``from_env()`` reproduces the field defaults —
    the same values every scattered call site falls back to today."""
    assert StrategyLabBudgetConfig.from_env() == StrategyLabBudgetConfig()


def test_defaults_preserve_documented_values() -> None:
    config = StrategyLabBudgetConfig.from_env()

    assert config.alignment_retries == 2
    assert config.llm_max_retries == 2
    assert config.llm_timeout_s == 3600.0
    assert config.llm_backoff_base_s == 2.0
    assert config.llm_backoff_max_s == 60.0
    assert config.design_review_rounds == 20
    assert config.design_review_stall_rounds == 3
    assert config.design_parse_retries == 2
    assert config.design_self_revision_rounds == 1
    assert config.design_max_llm_calls == 120
    assert config.refinement_parse_retries == 2
    assert config.refinement_stall_rounds == 3
    assert config.max_code_refinement_rounds == 50
    assert config.code_conformance_retries == 2
    assert config.max_alignment_rounds == 10


def test_default_total_budget_derives_from_retries_and_timeout() -> None:
    """Mirrors the envelope's own default: ``(max_retries + 1) * timeout * 1.5``."""
    config = StrategyLabBudgetConfig.from_env()
    assert config.llm_total_budget_s == pytest.approx(
        (config.llm_max_retries + 1) * config.llm_timeout_s * 1.5
    )


# ---------------------------------------------------------------------------
# from_env() overrides
# ---------------------------------------------------------------------------


def test_from_env_overrides_each_strategy_lab_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_RETRIES", "5")
    monkeypatch.setenv("STRATEGY_LAB_LLM_MAX_RETRIES", "4")
    monkeypatch.setenv("STRATEGY_LAB_LLM_TIMEOUT", "120")
    monkeypatch.setenv("STRATEGY_LAB_LLM_BACKOFF_BASE", "3")
    monkeypatch.setenv("STRATEGY_LAB_LLM_BACKOFF_MAX", "90")
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", "10")
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX", "50")
    monkeypatch.setenv("STRATEGY_LAB_LLM_TOTAL_BUDGET", "999")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "7")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "4")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "1")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "2")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "30")
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "6")
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", "8")
    monkeypatch.setenv("STRATEGY_LAB_MAX_CODE_REFINEMENT_ROUNDS", "9")
    monkeypatch.setenv("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", "1")
    monkeypatch.setenv("STRATEGY_LAB_MAX_ALIGNMENT_ROUNDS", "11")

    config = StrategyLabBudgetConfig.from_env()

    assert config.alignment_retries == 5
    assert config.llm_max_retries == 4
    assert config.llm_timeout_s == 120.0
    assert config.llm_backoff_base_s == 3.0
    assert config.llm_backoff_max_s == 90.0
    assert config.llm_rate_limit_backoff_initial_s == 10.0
    assert config.llm_rate_limit_backoff_max_s == 50.0
    assert config.llm_total_budget_s == 999.0
    assert config.design_review_rounds == 7
    assert config.design_review_stall_rounds == 4
    assert config.design_parse_retries == 1
    assert config.design_self_revision_rounds == 2
    assert config.design_max_llm_calls == 30
    assert config.refinement_parse_retries == 6
    assert config.refinement_stall_rounds == 8
    assert config.max_code_refinement_rounds == 9
    assert config.code_conformance_retries == 1
    assert config.max_alignment_rounds == 11


def test_llm_max_retries_falls_back_to_generic_llm_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STRATEGY_LAB_LLM_MAX_RETRIES`` unset falls back to ``LLM_MAX_RETRIES``
    before the ``2`` default, mirroring the existing envelope cascade."""
    monkeypatch.setenv("LLM_MAX_RETRIES", "9")
    assert StrategyLabBudgetConfig.from_env().llm_max_retries == 9


def test_garbage_env_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "not-a-number")
    assert StrategyLabBudgetConfig.from_env().design_review_rounds == 20


def test_sub_floor_env_value_is_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "0")
    assert StrategyLabBudgetConfig.from_env().design_review_rounds == 1

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "-3")
    assert StrategyLabBudgetConfig.from_env().design_parse_retries == 0


def test_rate_limit_max_floors_at_initial_when_max_env_is_lower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured cap below the initial can never leave the cap < initial."""
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", "50")
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX", "5")

    config = StrategyLabBudgetConfig.from_env()

    assert config.llm_rate_limit_backoff_initial_s == 50.0
    assert config.llm_rate_limit_backoff_max_s == 50.0


@pytest.mark.parametrize(
    "generic_env_var, sub_floor_value",
    [
        ("LLM_MAX_RETRIES", "-1"),
        ("LLM_BACKOFF_BASE", "0.5"),
        ("LLM_BACKOFF_MAX", "-1"),
        ("LLM_TIMEOUT", "0.0000001"),
        ("LLM_RATE_LIMIT_BACKOFF_INITIAL", "0.5"),
    ],
)
def test_sub_floor_generic_fallback_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, generic_env_var: str, sub_floor_value: str
) -> None:
    """A sub-floor generic ``LLM_*``/platform fallback must not turn an unset
    ``STRATEGY_LAB_*`` override into a ``ValueError`` — the generic fallback
    is clamped to the same floor before being used as the outer default."""
    monkeypatch.setenv(generic_env_var, sub_floor_value)
    StrategyLabBudgetConfig.from_env()  # must not raise


def test_strategy_lab_override_still_wins_over_sub_floor_generic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The STRATEGY_LAB_* override is honored even when the unused generic
    fallback would itself have been sub-floor."""
    monkeypatch.setenv("LLM_MAX_RETRIES", "-1")
    monkeypatch.setenv("STRATEGY_LAB_LLM_MAX_RETRIES", "7")
    assert StrategyLabBudgetConfig.from_env().llm_max_retries == 7


# ---------------------------------------------------------------------------
# Direct construction / validation
# ---------------------------------------------------------------------------


def test_direct_construction_accepts_explicit_overrides() -> None:
    config = StrategyLabBudgetConfig(design_review_rounds=1, max_alignment_rounds=1)
    assert config.design_review_rounds == 1
    assert config.max_alignment_rounds == 1


@pytest.mark.parametrize(
    "field_name, bad_value",
    [
        ("alignment_retries", -1),
        ("llm_max_retries", -1),
        ("llm_timeout_s", 0.0),
        ("llm_backoff_base_s", 0.5),
        ("llm_backoff_max_s", -1.0),
        ("llm_rate_limit_backoff_initial_s", 0.0),
        ("llm_total_budget_s", 0.0),
        ("design_review_rounds", 0),
        ("design_review_stall_rounds", 0),
        ("design_parse_retries", -1),
        ("design_self_revision_rounds", -1),
        ("design_max_llm_calls", 0),
        ("refinement_parse_retries", -1),
        ("refinement_stall_rounds", 0),
        ("max_code_refinement_rounds", 0),
        ("code_conformance_retries", -1),
        ("max_alignment_rounds", 0),
    ],
)
def test_construction_rejects_out_of_bounds_field(field_name: str, bad_value: float) -> None:
    with pytest.raises(ValueError, match=field_name):
        StrategyLabBudgetConfig(**{field_name: bad_value})


def test_construction_rejects_rate_limit_max_below_initial() -> None:
    with pytest.raises(ValueError, match="llm_rate_limit_backoff_max_s"):
        StrategyLabBudgetConfig(
            llm_rate_limit_backoff_initial_s=50.0, llm_rate_limit_backoff_max_s=10.0
        )


def test_config_is_frozen() -> None:
    config = StrategyLabBudgetConfig()
    with pytest.raises(AttributeError):
        config.design_review_rounds = 99  # type: ignore[misc]
