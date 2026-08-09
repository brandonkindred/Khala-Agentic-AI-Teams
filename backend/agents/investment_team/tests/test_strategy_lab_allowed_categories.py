"""Tests for user-selectable asset categories in the Strategy Lab.

The Strategy Lab UI lets the user constrain which asset categories the design
agent may generate strategies for. The selection rides on
``RunStrategyLabRequest.allowed_asset_classes`` and is translated into the
design pipeline's existing ``exclude_asset_classes`` constraint by
``build_strategy_lab_batch_input`` before the Temporal batch workflow starts.
These tests cover:

  * normalization of the raw selection to canonical, ideation-valid labels,
  * the allowed → excluded complement,
  * request-model validation (including the empty-after-normalization reject),
  * mix-hint steering restricted to the allowed classes, and
  * ``build_strategy_lab_batch_input`` computing the exclusion for the batch.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from investment_team.api import main as lab_main
from investment_team.api.main import RunStrategyLabRequest
from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, StopLossRule
from investment_team.strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    asset_class_mix_hint,
    excluded_for_allowed,
    normalize_allowed_asset_classes,
)

# ---------------------------------------------------------------------------
# normalize_allowed_asset_classes
# ---------------------------------------------------------------------------


def test_normalize_none_returns_none() -> None:
    """``None`` means "no constraint" and must propagate as ``None``."""
    assert normalize_allowed_asset_classes(None) is None


def test_normalize_maps_aliases_to_canonical_labels() -> None:
    out = normalize_allowed_asset_classes(["stock", "fx", "equity", "cryptocurrency"])
    # stock/equity → stocks (deduped), fx → forex, cryptocurrency → crypto.
    assert out == ["stocks", "crypto", "forex"]


def test_normalize_preserves_canonical_order_and_dedupes() -> None:
    out = normalize_allowed_asset_classes(["futures", "crypto", "crypto", "stocks"])
    assert out == ["stocks", "crypto", "futures"]


def test_normalize_drops_options_and_unknown_tokens() -> None:
    # options is canonical but not an ideation target; "bonds"/"" are unknown.
    out = normalize_allowed_asset_classes(["forex", "options", "bonds", ""])
    assert out == ["forex"]


def test_normalize_all_invalid_returns_empty_list() -> None:
    # A non-empty selection that resolves to nothing valid → empty list (the
    # request validator rejects this, but the helper itself stays total).
    assert normalize_allowed_asset_classes(["options", "bonds"]) == []


def test_normalize_empty_list_returns_empty_list() -> None:
    # An explicit empty list (distinct from None) is a non-None "selection" that
    # yields no allowed classes — the helper returns [] (not None); the request
    # validator turns that into a 422.
    assert normalize_allowed_asset_classes([]) == []


# ---------------------------------------------------------------------------
# excluded_for_allowed
# ---------------------------------------------------------------------------


def test_excluded_is_complement_within_prompt_classes() -> None:
    assert excluded_for_allowed(["forex"]) == ["stocks", "crypto", "futures", "commodities"]


def test_excluded_empty_when_all_classes_allowed() -> None:
    assert excluded_for_allowed(list(PROMPT_ASSET_CLASSES)) == []


def test_excluded_none_returns_empty() -> None:
    # ``None`` (no constraint, e.g. from normalize_allowed_asset_classes) excludes
    # nothing rather than raising — the function stays total against misuse.
    assert excluded_for_allowed(None) == []


# ---------------------------------------------------------------------------
# RunStrategyLabRequest.allowed_asset_classes validation
# ---------------------------------------------------------------------------


def test_request_default_is_none() -> None:
    assert RunStrategyLabRequest().allowed_asset_classes is None


def test_request_normalizes_selection() -> None:
    req = RunStrategyLabRequest(allowed_asset_classes=["stock", "fx"])
    assert req.allowed_asset_classes == ["stocks", "forex"]


def test_request_accepts_full_selection() -> None:
    req = RunStrategyLabRequest(allowed_asset_classes=list(PROMPT_ASSET_CLASSES))
    assert req.allowed_asset_classes == list(PROMPT_ASSET_CLASSES)


def test_request_rejects_selection_with_no_valid_category() -> None:
    # Pydantic surfaces the field validator's ValueError as a ValidationError
    # at model construction; catch that specific type rather than bare Exception
    # so an unexpected error (e.g. a validator bug) fails the test loudly.
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(allowed_asset_classes=["options"])
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(allowed_asset_classes=["bonds", "nonsense"])


def test_request_rejection_message_reflects_canonical_classes() -> None:
    """The error message is built from PROMPT_ASSET_CLASSES (the canonical
    source), not a hardcoded copy that could drift out of sync with it."""
    with pytest.raises(ValidationError) as exc_info:
        RunStrategyLabRequest(allowed_asset_classes=["options"])
    message = str(exc_info.value)
    for cls in PROMPT_ASSET_CLASSES:
        assert cls in message


# ---------------------------------------------------------------------------
# RunStrategyLabRequest.paper_trading_lookback_days bounds
# ---------------------------------------------------------------------------


def test_paper_trading_lookback_days_accepts_within_ceiling() -> None:
    from investment_team.strategy_lab.config import MAX_PAPER_TRADING_LOOKBACK_DAYS

    req = RunStrategyLabRequest(paper_trading_lookback_days=MAX_PAPER_TRADING_LOOKBACK_DAYS)
    assert req.paper_trading_lookback_days == MAX_PAPER_TRADING_LOOKBACK_DAYS


def test_paper_trading_lookback_days_rejects_above_ceiling() -> None:
    from investment_team.strategy_lab.config import MAX_PAPER_TRADING_LOOKBACK_DAYS

    with pytest.raises(ValidationError):
        RunStrategyLabRequest(paper_trading_lookback_days=MAX_PAPER_TRADING_LOOKBACK_DAYS + 1)


def test_paper_trading_lookback_days_default_within_cap() -> None:
    """The omitted lookback default must never exceed the configured schema
    ceiling — Pydantic v2 doesn't validate defaults, so the default itself
    has to be derived from the cap (same pattern as ``max_parallel``)."""
    from investment_team.api import main as api_main

    default_days = api_main.RunStrategyLabRequest().paper_trading_lookback_days
    assert default_days == min(365, api_main._MAX_PAPER_TRADING_LOOKBACK_DAYS)
    assert 30 <= default_days <= api_main._MAX_PAPER_TRADING_LOOKBACK_DAYS


def test_paper_trading_lookback_default_clamped_under_lowered_env_ceiling() -> None:
    """Omitting the field must still respect a lowered env ceiling.

    Preconditions: a fresh Python process can import ``investment_team`` with
    ``agents/`` on ``PYTHONPATH`` and
    ``STRATEGY_LAB_MAX_PAPER_TRADING_LOOKBACK_DAYS=90``.
    Postconditions: the child exits 0; an omitted
    ``paper_trading_lookback_days`` equals 90 (not the bare 365 default).
    Runs in a subprocess so the import-time Field default/``le=`` bind against
    the lowered ceiling without polluting this session's ``sys.modules``.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[3]
    agents_root = backend_root / "agents"
    script = """
from investment_team.api.main import RunStrategyLabRequest, _MAX_PAPER_TRADING_LOOKBACK_DAYS
assert _MAX_PAPER_TRADING_LOOKBACK_DAYS == 90, _MAX_PAPER_TRADING_LOOKBACK_DAYS
req = RunStrategyLabRequest()
assert req.paper_trading_lookback_days == 90, req.paper_trading_lookback_days
"""
    env = os.environ.copy()
    env["STRATEGY_LAB_MAX_PAPER_TRADING_LOOKBACK_DAYS"] = "90"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(agents_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(backend_root),
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_max_paper_trading_lookback_days_floors_below_field_ge() -> None:
    """Env ceiling below the Field ``ge=30`` must floor to 30 so ``le`` cannot
    fall below ``ge`` and make every request unsatisfiable.

    Runs in a subprocess so import-time config evaluation sees the lowered env
    without reloading modules already imported by this session.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[3]
    agents_root = backend_root / "agents"
    script = """
from investment_team.strategy_lab.config import MAX_PAPER_TRADING_LOOKBACK_DAYS
assert MAX_PAPER_TRADING_LOOKBACK_DAYS == 30, MAX_PAPER_TRADING_LOOKBACK_DAYS
"""
    env = os.environ.copy()
    env["STRATEGY_LAB_MAX_PAPER_TRADING_LOOKBACK_DAYS"] = "10"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(agents_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(backend_root),
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# ---------------------------------------------------------------------------
# asset_class_mix_hint(..., exclude=...)
# ---------------------------------------------------------------------------


def _stub_backtest_result() -> BacktestResult:
    return BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=5.0,
        volatility_pct=12.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=-3.0,
        win_rate_pct=55.0,
        profit_factor=1.2,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _record(asset_class: str) -> StrategyLabRecord:
    suffix = uuid.uuid4().hex[:6]
    strategy = StrategySpec(
        strategy_id=f"s-{suffix}",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    now = lab_main._now()
    backtest = BacktestRecord(
        backtest_id=f"bt-{suffix}",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-12-31"),
        submitted_by="test",
        submitted_at=now,
        completed_at=now,
        status="completed",
        result=_stub_backtest_result(),
        notes=[],
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=f"lab-{suffix}",
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


def test_mix_hint_exclude_restricts_menu_when_no_records() -> None:
    out = asset_class_mix_hint([], exclude=["stocks", "crypto", "futures", "commodities"])
    # Only the single allowed class is offered, and no excluded class appears
    # anywhere in the hint (the stocks-nudge is also dropped when stocks is
    # excluded, so this stays robust to wording rather than pinning exact text).
    assert "forex" in out
    for excluded in ("stocks", "crypto", "futures", "commodities"):
        assert excluded not in out


def test_mix_hint_exclude_drops_excluded_classes_from_counts() -> None:
    records = [_record("forex") for _ in range(3)] + [_record("crypto") for _ in range(2)]
    out = asset_class_mix_hint(records, exclude=["stocks", "futures", "commodities"])
    # Allowed classes are counted; excluded classes never appear in the counts.
    assert "forex=3" in out
    assert "crypto=2" in out
    assert "stocks=" not in out
    assert "futures=" not in out
    assert "commodities=" not in out


def test_mix_hint_excluded_class_priors_not_counted_as_stocks() -> None:
    # Regression: a prior in an *excluded* but valid class (crypto, here) must be
    # skipped — not folded into the stocks count. Folding it would inflate stocks
    # and wrongly steer the model away from stocks even though stocks is allowed.
    records = [_record("stocks") for _ in range(2)] + [_record("crypto") for _ in range(5)]
    out = asset_class_mix_hint(records, exclude=["crypto", "futures", "commodities"])
    # Only the two genuine stocks priors count; the five excluded crypto priors
    # are outside the steering window.
    assert "stocks=2" in out
    assert "forex=0" in out
    # crypto must not appear in the counts at all (neither as its own line nor
    # absorbed into stocks → which would have read stocks=7).
    assert "crypto" not in out
    assert "stocks=7" not in out
    assert "Equities are relatively heavy" not in out


def test_mix_hint_no_exclude_matches_unconstrained() -> None:
    """An empty exclusion must reproduce the unconstrained hint verbatim."""
    records = [_record("stocks") for _ in range(4)]
    assert asset_class_mix_hint(records, exclude=[]) == asset_class_mix_hint(records)
    assert asset_class_mix_hint([], exclude=None) == asset_class_mix_hint([])


def test_mix_hint_drops_stocks_nudge_when_stocks_excluded() -> None:
    # No-records menu must not tell the model "do not default to stocks" when
    # stocks isn't even a permitted choice.
    out = asset_class_mix_hint([], exclude=["stocks"])
    assert "do **not** default to stocks" not in out
    assert "stocks" not in out
    assert "pick the class that best fits your multi-signal story" in out


def test_mix_hint_keeps_stocks_nudge_when_stocks_allowed() -> None:
    # When stocks is still allowed, the anti-bias nudge stays (unconstrained
    # output is unchanged by the exclusion plumbing).
    assert "do **not** default to stocks" in asset_class_mix_hint([])
    assert "do **not** default to stocks" in asset_class_mix_hint([], exclude=["crypto"])


# ---------------------------------------------------------------------------
# build_strategy_lab_batch_input: allowed_asset_classes → exclude_asset_classes
# ---------------------------------------------------------------------------


def _build_batch_input(
    monkeypatch: pytest.MonkeyPatch, request: RunStrategyLabRequest
) -> Dict[str, Any]:
    from investment_team.strategy_lab import run_state
    from investment_team.strategy_lab.temporal.start_workflow import (
        build_strategy_lab_batch_input,
    )

    monkeypatch.setattr(run_state, "active_runs", {})
    # rehydrate_active_run_offset/get_resume_seed_counters read via
    # get_run_state_strict (not the lenient get_run_state/
    # load_run_from_job_service) -- see its own docstring for why a
    # durable-read failure must propagate rather than being swallowed here.
    monkeypatch.setattr(run_state, "get_run_state_strict", lambda rid: None)

    run_id = f"run-{uuid.uuid4().hex[:6]}"
    return build_strategy_lab_batch_input(run_id, request, generation=1)


def test_batch_input_computes_complement_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
        allowed_asset_classes=["forex"],
    )
    batch_input = _build_batch_input(monkeypatch, request)
    assert batch_input["exclude_asset_classes"] == ["stocks", "crypto", "futures", "commodities"]


def test_batch_input_no_exclusion_when_all_categories_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RunStrategyLabRequest(
        batch_size=1,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
        allowed_asset_classes=list(PROMPT_ASSET_CLASSES),
    )
    batch_input = _build_batch_input(monkeypatch, request)
    assert batch_input["exclude_asset_classes"] is None


def test_batch_input_no_exclusion_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    request = RunStrategyLabRequest(
        batch_size=1,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    batch_input = _build_batch_input(monkeypatch, request)
    assert batch_input["exclude_asset_classes"] is None


# ---------------------------------------------------------------------------
# GET /strategy-lab/config — surfaces the category list to the UI
# ---------------------------------------------------------------------------


def test_config_endpoint_exposes_asset_categories() -> None:
    # The UI sources its category selector from this list, so it must match the
    # backend's ideation-valid classes exactly.
    cfg = lab_main.get_strategy_lab_config()
    assert cfg.asset_categories == list(PROMPT_ASSET_CLASSES)
