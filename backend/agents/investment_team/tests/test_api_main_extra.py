"""Additional coverage for ``investment_team.api.main`` helpers + routes.

Builds on ``test_api_routes`` and ``test_investment_team``'s cycle
fixtures. Targets:

* ``_PersistentDict`` __setitem__/__getitem__/__contains__/pop/values
  via an in-process FakeJobClient, including __setitem__'s atomic-upsert
  contract (no get_job read-before-write), concurrent same-key writes, and
  pop's lost-delete-race handling (a concurrent pop() already won).
* ``_env_positive_int`` env-var parsing.
* ``_normalize_strategy_lab_asset_class`` + ``_build_strategy_from_ideation``
  builders.
* ``_run_backtest_background`` happy + InvestmentBacktestError + generic-exception
  + early-cancel branches.
* ``_purge_strategy_lab_job_storage`` + ``_delete_paper_sessions_for_lab_record``.
* ``_resolve_fee_overrides`` (0.0 sentinel handling).
* ``_recover_orphaned_paper_trading_sessions`` startup hook.
* ``_load_run_from_job_service`` fallback + ``_persist_run_state``
  propagating job-service errors and not clobbering status on a
  status-less update.
* ``_strategy_lab_signal_expert_enabled`` env-var toggle.
* ``run_paper_trading`` validation branches (not-winning / no strategy_code)
  + happy path with patched background worker.
* ``stream_strategy_lab_run`` terminal-state short-circuit (404 + immediate
  done).
* ``delete_strategy_lab_record`` success path.
* ``complete_advisor_session`` happy path.
* ``RunStrategyLabRequest`` batch_size/batch_count bounds.
* ``acquire_run_transition_lock`` per-run_id serialization primitive.
* ``run_state.get_run_generation_strict`` and ``_build_run_state``'s
  ``generation`` field, plus ``_legacy_generation_bootstrap_increment``
  (generation-fencing coverage).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

import httpx
import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clear_job_client_cache():
    """Isolate _PersistentDict.__init__'s get_job_service_client delegation.

    _PersistentDict.__init__ now resolves its client through the process-wide
    ``job_service_client`` cache (one client per team, for the life of the
    process) rather than constructing a fresh JobServiceClient per instance.
    Clearing the cache before and after each test keeps every
    ``_PersistentDict(...)`` construction in this file deterministic,
    regardless of team-string reuse or test execution order.
    """
    import job_service_client as jsc

    jsc._clear_job_client_cache_for_testing()
    yield
    jsc._clear_job_client_cache_for_testing()


if TYPE_CHECKING:
    from investment_team.models import StrategyLabRecord


def test_api_main_has_no_module_level_logging_basic_config_call() -> None:
    """A top-level ``logging.basicConfig(...)`` statement in this module would
    mutate the global root logger as a side effect of merely importing it,
    overriding the application entrypoint's (or a test runner's) intended
    logging setup depending on import order.

    Statically inspects the module's top-level statements (rather than
    reimporting it) because ``investment_team.api.main`` has real import-time
    side effects of its own (``create_team_app(...)``, module-level
    singletons) that a forced reimport would re-trigger and that other
    modules alias by identity (see ``test_orchestrator_api``'s
    ``_DEFERRED``-symbol aliasing checks) — reloading would silently break
    those instead of testing this module's logging behavior.
    """
    import ast
    import inspect

    from investment_team.api import main as api_main

    tree = ast.parse(inspect.getsource(api_main))
    offending = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "basicConfig"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "logging"
    ]
    assert not offending, (
        "Module-level logging.basicConfig call reintroduced in "
        "investment_team.api.main — this mutates the global root logger "
        "as a side effect of import."
    )


def test_clamp_max_parallel_caps_to_env_ceiling(monkeypatch, caplog) -> None:
    """The Strategy Lab concurrency clamp bounds a request's max_parallel to the
    env-configured ceiling and logs only when it actually lowers the value."""
    import logging

    from investment_team.strategy_lab import config

    monkeypatch.setattr(config, "MAX_CONCURRENT_CYCLES", 2)
    assert config.clamp_max_parallel(1) == 1  # below cap → unchanged
    assert config.clamp_max_parallel(2) == 2  # at cap → unchanged
    with caplog.at_level(logging.INFO, logger=config.logger.name):
        assert config.clamp_max_parallel(5) == 2  # above cap → clamped + logged
    assert any("concurrency capped to 2" in r.getMessage() for r in caplog.records)

    # Default (cap == MAX_PARALLEL) imposes no extra constraint up to the schema max.
    monkeypatch.setattr(config, "MAX_CONCURRENT_CYCLES", config.MAX_PARALLEL)
    assert config.clamp_max_parallel(config.MAX_PARALLEL) == config.MAX_PARALLEL


def test_run_strategy_lab_request_default_within_cap() -> None:
    """The omitted max_parallel default must never exceed the configured schema
    ceiling (_MAX_PARALLEL) — Pydantic v2 doesn't validate defaults, so the default
    itself has to be derived from the cap."""
    from investment_team.api import main as api_main

    default_mp = api_main.RunStrategyLabRequest().max_parallel
    assert default_mp == min(3, api_main._MAX_PARALLEL)
    assert 1 <= default_mp <= api_main._MAX_PARALLEL


def test_run_strategy_lab_request_total_cycles_is_batch_size_times_batch_count() -> None:
    """Sanity check: the request validates and computes total work correctly,
    and its batch_size/batch_count bounds (including the operator-tunable
    _MAX_BATCH_COUNT ceiling) are enforced."""
    from pydantic import ValidationError

    from investment_team.api import main as api_main

    request = api_main.RunStrategyLabRequest(batch_size=5, batch_count=4)
    assert request.batch_size * request.batch_count == 20

    with pytest.raises(ValidationError):
        api_main.RunStrategyLabRequest(batch_size=0)
    with pytest.raises(ValidationError):
        api_main.RunStrategyLabRequest(batch_count=0)
    with pytest.raises(ValidationError):
        api_main.RunStrategyLabRequest(batch_count=api_main._MAX_BATCH_COUNT + 1)


def _stub_backtest_result():
    from investment_team.models import BacktestResult

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


def _make_backtest_record(record_id: str, *, completed_at: str = "2024-01-01T00:00:00Z"):
    from investment_team.models import BacktestConfig, BacktestRecord, StrategySpec

    strategy = StrategySpec(
        strategy_id=f"strat-{record_id}",
        authored_by="tester",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(start_date="2024-01-01", end_date="2024-06-01")
    return BacktestRecord(
        backtest_id=record_id,
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=config,
        submitted_by="tester",
        submitted_at=completed_at,
        completed_at=completed_at,
        result=_stub_backtest_result(),
    )


def _make_strategy_lab_record(record_id: str, *, is_winning: bool = False):
    from investment_team.models import StrategyLabRecord

    now = "2024-01-01T00:00:00Z"
    backtest = _make_backtest_record(f"bt-{record_id}", completed_at=now)
    return StrategyLabRecord(
        lab_record_id=record_id,
        strategy=backtest.strategy,
        backtest=backtest,
        is_winning=is_winning,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
    )


def test_list_backtests_response_derives_count_from_items() -> None:
    """count is enforced by a model_validator, so a mismatched constructor
    value can never survive construction."""
    from investment_team.api.main import ListBacktestsResponse

    record = _make_backtest_record("bt-1")
    resp = ListBacktestsResponse(items=[record], count=999)
    assert resp.count == 1

    empty = ListBacktestsResponse(items=[], count=5)
    assert empty.count == 0


def test_strategy_lab_run_response_derives_count_from_records() -> None:
    from investment_team.api.main import StrategyLabRunResponse

    record = _make_strategy_lab_record("lab-1")
    resp = StrategyLabRunResponse(records=[record], count=999)
    assert resp.count == 1

    empty = StrategyLabRunResponse(records=[], count=5)
    assert empty.count == 0


def test_strategy_lab_results_response_derives_counts_from_items() -> None:
    """count/winning_count/losing_count are all derived from items, so a
    mismatched constructor value can never survive construction — and a
    filtered items list (e.g. ?winning=true) correctly reports losing_count
    == 0 rather than an unfiltered global count."""
    from investment_team.api.main import StrategyLabResultsResponse

    winner = _make_strategy_lab_record("lab-w", is_winning=True)
    loser = _make_strategy_lab_record("lab-l", is_winning=False)

    resp = StrategyLabResultsResponse(
        items=[winner, loser], count=999, winning_count=0, losing_count=0
    )
    assert resp.count == 2
    assert resp.winning_count == 1
    assert resp.losing_count == 1

    filtered = StrategyLabResultsResponse(items=[winner])
    assert filtered.count == 1
    assert filtered.winning_count == 1
    assert filtered.losing_count == 0


class _InMemoryDict(MutableMapping):
    """Plain-dict stand-in for monkeypatching api.main's module-level record
    stores. Subclasses MutableMapping (rather than hand-rolling the dict
    protocol) so it gets correct semantics -- including __iter__/__len__/
    keys/items/update/setdefault and a real KeyError on deleting a missing
    key -- for free, matching what production code calling these stores
    would see from an actual dict."""

    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def __delitem__(self, k):
        del self._d[k]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)


def test_in_memory_dict_matches_real_dict_protocol() -> None:
    """Regression: the hand-rolled predecessor of this MutableMapping-based
    test double was missing __iter__/__len__/keys/items/update/setdefault,
    and its __delitem__ silently no-op'd on a missing key instead of
    raising KeyError like a real dict -- gaps that could mask a bug in
    production code exercising the full mapping protocol against these
    monkeypatched stores."""
    d = _InMemoryDict()
    d["a"] = 1
    d.setdefault("b", 2)
    d.update({"c": 3})
    assert len(d) == 3
    assert set(iter(d)) == {"a", "b", "c"}
    assert dict(d.items()) == {"a": 1, "b": 2, "c": 3}
    assert set(d.keys()) == {"a", "b", "c"}
    with pytest.raises(KeyError):
        del d["missing"]


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_profiles", _InMemoryDict())
    monkeypatch.setattr(api_main, "_proposals", _InMemoryDict())
    monkeypatch.setattr(api_main, "_strategies", _InMemoryDict())
    monkeypatch.setattr(api_main, "_validations", _InMemoryDict())
    monkeypatch.setattr(api_main, "_backtests", _InMemoryDict())
    monkeypatch.setattr(api_main, "_strategy_lab_records", _InMemoryDict())
    monkeypatch.setattr(api_main, "_paper_trading_sessions", _InMemoryDict())
    monkeypatch.setattr(api_main, "_advisor_sessions", _InMemoryDict())
    from investment_team.orchestrator import WorkflowState

    monkeypatch.setattr(api_main, "_workflow_state", WorkflowState())
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# _env_positive_int
# ---------------------------------------------------------------------------


def test_env_positive_int_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.config import env_positive_int

    monkeypatch.delenv("MY_TEST_INT", raising=False)
    assert env_positive_int("MY_TEST_INT", 7) == 7


def test_env_positive_int_returns_default_on_non_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.strategy_lab.config import env_positive_int

    monkeypatch.setenv("MY_TEST_INT", "not-a-number")
    assert env_positive_int("MY_TEST_INT", 5) == 5


def test_env_positive_int_returns_default_on_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.config import env_positive_int

    monkeypatch.setenv("MY_TEST_INT", "0")
    assert env_positive_int("MY_TEST_INT", 3) == 3


def test_env_positive_int_returns_parsed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.config import env_positive_int

    monkeypatch.setenv("MY_TEST_INT", "42")
    assert env_positive_int("MY_TEST_INT", 1) == 42


# ---------------------------------------------------------------------------
# acquire_run_transition_lock
# ---------------------------------------------------------------------------
#
# Each test uses a fresh uuid4 run_id so tests never contend with each other
# via the shared (never-evicted) run_state._run_transition_locks registry.


def _fresh_run_id() -> str:
    return f"run-transition-lock-test-{uuid.uuid4().hex}"


def test_acquire_run_transition_lock_returns_lock_when_free() -> None:
    from investment_team.strategy_lab.run_state import acquire_run_transition_lock

    run_id = _fresh_run_id()
    run_lock = acquire_run_transition_lock(run_id)
    assert run_lock is not None
    assert run_lock.locked()
    run_lock.release()


def test_acquire_run_transition_lock_returns_none_when_already_held() -> None:
    from investment_team.strategy_lab.run_state import acquire_run_transition_lock

    run_id = _fresh_run_id()
    first = acquire_run_transition_lock(run_id)
    assert first is not None
    try:
        second = acquire_run_transition_lock(run_id)
        assert second is None
    finally:
        first.release()


def test_acquire_run_transition_lock_reacquire_after_release() -> None:
    from investment_team.strategy_lab.run_state import acquire_run_transition_lock

    run_id = _fresh_run_id()
    first = acquire_run_transition_lock(run_id)
    assert first is not None
    first.release()

    second = acquire_run_transition_lock(run_id)
    assert second is not None
    second.release()


def test_acquire_run_transition_lock_different_run_ids_independent() -> None:
    from investment_team.strategy_lab.run_state import acquire_run_transition_lock

    run_id_a = _fresh_run_id()
    run_id_b = _fresh_run_id()
    lock_a = acquire_run_transition_lock(run_id_a)
    lock_b = acquire_run_transition_lock(run_id_b)
    try:
        assert lock_a is not None
        assert lock_b is not None
        assert lock_a is not lock_b
    finally:
        lock_a.release()
        lock_b.release()


def test_acquire_run_transition_lock_same_run_id_returns_same_object() -> None:
    """Regression guard for the registry's core invariant: two callers for
    the same run_id must contend for the SAME Lock instance, not two
    different ones — otherwise both would "acquire" independently and the
    guard would silently stop serializing anything."""
    from investment_team.strategy_lab.run_state import (
        _run_transition_locks,
        acquire_run_transition_lock,
    )

    run_id = _fresh_run_id()
    run_lock = acquire_run_transition_lock(run_id)
    assert run_lock is not None
    try:
        assert _run_transition_locks[run_id] is run_lock
    finally:
        run_lock.release()


def test_acquire_run_transition_lock_concurrent_same_run_id_exactly_one_wins() -> None:
    """Real multi-thread contention test of the primitive itself (not a
    probabilistic torn-read — this is the one case where actual OS-thread
    concurrency is the right tool, since acquire(blocking=False) is meant to
    behave correctly under genuine simultaneous callers)."""
    from investment_team.strategy_lab.run_state import acquire_run_transition_lock

    run_id = _fresh_run_id()
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results: List[Any] = [None] * n_threads

    def _worker(idx: int) -> None:
        barrier.wait()
        results[idx] = acquire_run_transition_lock(run_id)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    winners[0].release()


# ---------------------------------------------------------------------------
# _strategy_lab_signal_expert_enabled
# ---------------------------------------------------------------------------


def test_strategy_lab_signal_expert_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.api.main import _strategy_lab_signal_expert_enabled

    monkeypatch.delenv("STRATEGY_LAB_SIGNAL_EXPERT_ENABLED", raising=False)
    assert _strategy_lab_signal_expert_enabled() is True


def test_strategy_lab_signal_expert_enabled_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.api.main import _strategy_lab_signal_expert_enabled

    monkeypatch.setenv("STRATEGY_LAB_SIGNAL_EXPERT_ENABLED", "false")
    assert _strategy_lab_signal_expert_enabled() is False


# ---------------------------------------------------------------------------
# _live_paper_enabled + _resolve_fee_overrides
# ---------------------------------------------------------------------------


def test_live_paper_enabled_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.api.main import _live_paper_enabled

    monkeypatch.delenv("INVESTMENT_LIVE_PAPER_ENABLED", raising=False)
    assert _live_paper_enabled() is False


def test_live_paper_enabled_on_when_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.api.main import _live_paper_enabled

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    assert _live_paper_enabled() is True


def test_resolve_fee_overrides_zero_preserved() -> None:
    """``transaction_cost_bps=0.0`` is honoured (not coerced to default)."""
    from investment_team.api.main import RunPaperTradingRequest, _resolve_fee_overrides

    req = RunPaperTradingRequest(
        lab_record_id="lab-1",
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 0.0
    assert slip == 0.0


def test_resolve_fee_overrides_defaults_when_none() -> None:
    from investment_team.api.main import RunPaperTradingRequest, _resolve_fee_overrides

    req = RunPaperTradingRequest(
        lab_record_id="lab-1",
        transaction_cost_bps=None,
        slippage_bps=None,
    )
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 5.0  # _DEFAULT_TX_COST_BPS
    assert slip == 2.0  # _DEFAULT_SLIPPAGE_BPS


def test_resolve_fee_overrides_defaults_are_operator_tunable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback defaults are business parameters operators may need to
    retune without a redeploy — read from env vars, not hardcoded."""
    from investment_team.api.main import RunPaperTradingRequest, _resolve_fee_overrides

    monkeypatch.setenv("INVESTMENT_DEFAULT_TX_COST_BPS", "12.5")
    monkeypatch.setenv("INVESTMENT_DEFAULT_SLIPPAGE_BPS", "7.5")

    req = RunPaperTradingRequest(
        lab_record_id="lab-1",
        transaction_cost_bps=None,
        slippage_bps=None,
    )
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 12.5
    assert slip == 7.5


def test_resolve_fee_overrides_defaults_fall_back_on_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage env value falls back to the documented default rather than
    raising or silently propagating an unparseable value."""
    from investment_team.api.main import RunPaperTradingRequest, _resolve_fee_overrides

    monkeypatch.setenv("INVESTMENT_DEFAULT_TX_COST_BPS", "not-a-number")
    monkeypatch.setenv("INVESTMENT_DEFAULT_SLIPPAGE_BPS", "-5")

    req = RunPaperTradingRequest(
        lab_record_id="lab-1",
        transaction_cost_bps=None,
        slippage_bps=None,
    )
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 5.0  # unparseable -> falls back to the documented default
    assert slip == 0.0  # -5 is out of [0, 1000] -> clamped to the floor


# ---------------------------------------------------------------------------
# _parse_iso_timestamp_for_sort
# ---------------------------------------------------------------------------


def test_parse_iso_timestamp_for_sort_empty_string_sorts_last() -> None:
    from datetime import datetime, timezone

    from investment_team.api.main import _parse_iso_timestamp_for_sort

    assert _parse_iso_timestamp_for_sort("") == datetime.min.replace(tzinfo=timezone.utc)


def test_parse_iso_timestamp_for_sort_unparseable_falls_back_like_empty() -> None:
    from datetime import datetime, timezone

    from investment_team.api.main import _parse_iso_timestamp_for_sort

    assert _parse_iso_timestamp_for_sort("not-a-timestamp") == datetime.min.replace(
        tzinfo=timezone.utc
    )


def test_parse_iso_timestamp_for_sort_accepts_z_suffix() -> None:
    from datetime import datetime, timezone

    from investment_team.api.main import _parse_iso_timestamp_for_sort

    assert _parse_iso_timestamp_for_sort("2024-01-01T12:00:00Z") == datetime(
        2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_parse_iso_timestamp_for_sort_naive_string_assumed_utc() -> None:
    from datetime import datetime, timezone

    from investment_team.api.main import _parse_iso_timestamp_for_sort

    assert _parse_iso_timestamp_for_sort("2024-01-01T12:00:00") == datetime(
        2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_parse_iso_timestamp_for_sort_respects_non_utc_offset() -> None:
    from datetime import datetime, timezone

    from investment_team.api.main import _parse_iso_timestamp_for_sort

    # 2024-01-02T00:30:00+02:00 is 2024-01-01T22:30:00 UTC.
    parsed = _parse_iso_timestamp_for_sort("2024-01-02T00:30:00+02:00")
    assert parsed == datetime(2024, 1, 1, 22, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _normalize_strategy_lab_asset_class + _build_strategy_from_ideation
# ---------------------------------------------------------------------------


def test_build_strategy_from_ideation_round_trip() -> None:
    from investment_team.api.main import _build_strategy_from_ideation

    data = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1h",
        "entry_rules": [
            {"kind": "entry", "side": "long", "when": {"lhs": "bar.close", "op": ">", "rhs": 100.0}}
        ],
        "exit_rules": [{"kind": "stop_loss", "pct": 0.05}],
        "sizing": {"kind": "fixed_fraction", "fraction": 0.1},
        "risk_limits": {"max_position_pct": 5},
        "speculative": True,
    }
    strategy, sid = _build_strategy_from_ideation(data)
    assert sid.startswith("strat-lab-")
    # normalize_asset_class passes "stocks" through unchanged (it's already canonical).
    assert strategy.asset_class == "stocks"
    assert strategy.timeframe == "1h"
    assert strategy.speculative is True


def test_build_strategy_from_ideation_defaults_when_missing() -> None:
    from investment_team.api.main import _build_strategy_from_ideation

    # All fields missing — defaults must kick in (timeframe → "1d").
    data: Dict[str, Any] = {}
    strategy, sid = _build_strategy_from_ideation(data)
    assert strategy.timeframe == "1d"
    assert sid.startswith("strat-lab-")


def test_build_strategy_from_ideation_defaults_invalid_timeframe() -> None:
    """An LLM-returned timeframe outside StrategySpec's allowed literal set
    (e.g. a typo'd unit, or an empty string) must not raise a pydantic
    ValidationError -- it degrades to the same "1d" default as an omitted
    field, exactly like the missing-field case."""
    from investment_team.api.main import _build_strategy_from_ideation

    for bad_timeframe in ("1x", "", "daily", "1D"):
        data: Dict[str, Any] = {"timeframe": bad_timeframe}
        strategy, _ = _build_strategy_from_ideation(data)
        assert strategy.timeframe == "1d"


def test_build_strategy_from_ideation_discards_non_dict_rules() -> None:
    from investment_team.api.main import _build_strategy_from_ideation

    data = {
        "asset_class": "stocks",
        "timeframe": "1d",
        "entry_rules": ["not a dict", 42, None],
        "exit_rules": [{"kind": "stop_loss", "pct": 0.1}, "garbage"],
        "sizing": "not a dict — should fall back to default",
    }
    strategy, _ = _build_strategy_from_ideation(data)
    assert strategy.entry_rules == []
    assert len(strategy.exit_rules) == 1


def test_build_strategy_from_ideation_recovers_single_dict_rule() -> None:
    """A single dict emitted in place of a one-element list must be
    recovered, not silently discarded. ``strategy_data.get("entry_rules") or
    []`` previously evaluated a truthy dict to itself, and iterating a dict
    yields its string keys (none of which are dicts) -- filtering those out
    left an empty rule set with no warning, a silent data-loss path for
    otherwise-recoverable LLM output.
    """
    from investment_team.api.main import _build_strategy_from_ideation
    from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate

    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
    ).model_dump()
    data = {
        "asset_class": "stocks",
        "timeframe": "1d",
        "entry_rules": entry_rule,  # a bare dict, not wrapped in a list
        "exit_rules": {"kind": "stop_loss", "pct": 0.1},  # likewise
    }
    strategy, _ = _build_strategy_from_ideation(data)
    assert len(strategy.entry_rules) == 1
    assert strategy.entry_rules[0].side == "long"
    assert len(strategy.exit_rules) == 1
    assert strategy.exit_rules[0].pct == 0.1


def test_build_strategy_from_ideation_rejects_non_mapping() -> None:
    from investment_team.api.main import _build_strategy_from_ideation

    with pytest.raises(TypeError):
        _build_strategy_from_ideation(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _build_strategy_from_ideation(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _legacy_generation_bootstrap_increment
# ---------------------------------------------------------------------------


def test_legacy_generation_bootstrap_increment_jumps_to_two_when_field_absent() -> None:
    """A run with no persisted "generation" field (pre-fencing, or a missing
    job record passed as ``{}``) must bootstrap by +2, not +1 -- +1 would mint
    generation 1, which is also what a stale legacy activity that omits
    "generation" entirely is treated as presenting, defeating fencing.
    Transport failures on the bootstrap read fail closed at the route instead
    of being rewritten as ``{}`` here."""
    from investment_team.api.main import (
        GENERATION_INCREMENT_LEGACY_BOOTSTRAP,
        _legacy_generation_bootstrap_increment,
    )

    assert _legacy_generation_bootstrap_increment({}) == GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    assert (
        _legacy_generation_bootstrap_increment({"status": "completed"})
        == GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    )


@pytest.mark.parametrize(
    "uninitialized_generation",
    [None, "", 0, -1, "0", "-3", "not-an-int", [], {}],
)
def test_legacy_generation_bootstrap_increment_jumps_to_two_when_field_uninitialized(
    uninitialized_generation,
) -> None:
    """A present but uninitialized/unparseable generation must use the same +2
    bootstrap as a missing key -- get_run_generation_strict already treats
    None/empty/<=0 as DEFAULT_FENCING_GENERATION, and job-service increment
    coerces unparseable non-ints to 0, so +2 lands safely above the legacy
    activity default of 1."""
    from investment_team.api.main import (
        GENERATION_INCREMENT_LEGACY_BOOTSTRAP,
        _legacy_generation_bootstrap_increment,
    )

    assert (
        _legacy_generation_bootstrap_increment({"generation": uninitialized_generation})
        == GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    )


@pytest.mark.parametrize("unsafe_generation", ["5", "1", "42", True, False, 1.5, 2.0])
def test_legacy_generation_bootstrap_increment_fails_closed_on_non_native_positive_token(
    unsafe_generation,
) -> None:
    """A durable generation that parses as a positive fencing token but is not
    a native int (numeric string) — or is a bool/float get_run_generation_strict
    rejects — must raise rather than return +2: job-service increment would
    zero a string first and mint 2, regressing e.g. conceptual generation 5
    and letting in-flight activities that present 5 pass check_fencing_token."""
    from investment_team.api.main import (
        UnsafeDurableGenerationError,
        _legacy_generation_bootstrap_increment,
    )

    with pytest.raises(UnsafeDurableGenerationError):
        _legacy_generation_bootstrap_increment({"generation": unsafe_generation})


def test_legacy_generation_bootstrap_increment_normal_when_field_present() -> None:
    """A run that already has a persisted positive integer "generation" field
    (created after fencing shipped, or a legacy run past its first
    post-upgrade restart) increments by the ordinary amount rather than
    re-bootstrapping."""
    from investment_team.api.main import (
        GENERATION_INCREMENT_NORMAL,
        _legacy_generation_bootstrap_increment,
    )

    assert _legacy_generation_bootstrap_increment({"generation": 1}) == GENERATION_INCREMENT_NORMAL
    assert _legacy_generation_bootstrap_increment({"generation": 7}) == GENERATION_INCREMENT_NORMAL


# ---------------------------------------------------------------------------
# _persist_strategy_lab_record
# ---------------------------------------------------------------------------


def test_persist_strategy_lab_record_rejects_missing_strategy() -> None:
    """A record with strategy=None (bypassing Pydantic's own field validation
    via model_construct, as a stray in-process mutation elsewhere might) must
    raise ValueError before acquiring the lock -- not an AttributeError from
    inside the locked write."""
    from investment_team.api.main import _persist_strategy_lab_record
    from investment_team.models import BacktestRecord, StrategyLabRecord

    record = StrategyLabRecord.model_construct(
        lab_record_id="lab-missing-strategy",
        strategy=None,
        backtest=BacktestRecord.model_construct(backtest_id="bt-1"),
    )

    with pytest.raises(ValueError, match="record.strategy must be populated"):
        _persist_strategy_lab_record(record)


def test_persist_strategy_lab_record_rejects_missing_backtest() -> None:
    """Same contract check for a missing backtest."""
    from investment_team.api.main import _persist_strategy_lab_record
    from investment_team.models import StrategyLabRecord, StrategySpec

    record = StrategyLabRecord.model_construct(
        lab_record_id="lab-missing-backtest",
        strategy=StrategySpec.model_construct(strategy_id="strat-1"),
        backtest=None,
    )

    with pytest.raises(ValueError, match="record.backtest must be populated"):
        _persist_strategy_lab_record(record)


# ---------------------------------------------------------------------------
# _PersistentDict (in-process FakeJobClient roundtrip)
# ---------------------------------------------------------------------------


class _FakeJobClient:
    """Minimal in-memory ``JobServiceClient`` for tests that exercise
    job-service-backed helpers (e.g. ``_delete_paper_sessions_for_lab_record``,
    ``_purge_strategy_lab_job_storage``).

    Thread-safe: the purge/delete helpers under test issue ``delete_job``
    calls concurrently across a thread pool, so all mutations of ``_jobs`` are
    guarded by a lock to keep the in-memory store consistent under that fan-out.
    """

    def __init__(self, team: str = "x", base_url: str | None = None) -> None:
        self.team = team
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.get_job_calls = 0
        self.create_job_calls = 0
        self.update_job_calls = 0

    def get_job(self, job_id: str):
        with self._lock:
            self.get_job_calls += 1
            return dict(self._jobs[job_id]) if job_id in self._jobs else None

    def create_job(self, job_id: str, *, status: str = "stored", **fields):
        with self._lock:
            self.create_job_calls += 1
            self._jobs[job_id] = {"job_id": job_id, "status": status, **fields}

    def update_job(self, job_id: str, **fields):
        with self._lock:
            self.update_job_calls += 1
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def list_jobs(self, *, statuses=None):
        with self._lock:
            return [dict(j) for j in self._jobs.values()]


def test_persistent_dict_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set, get, contains, delete, pop, values on a _PersistentDict."""
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict
    from investment_team.models import (
        IPS,
        IncomeProfile,
        InvestmentProfile,
        LiquidityNeeds,
        NetWorth,
        PortfolioConstraints,
        RiskTolerance,
        SavingsRate,
        TaxProfile,
        UserPreferences,
    )

    pd = _PersistentDict("profiles_test")

    # Build a minimal-but-valid IPS for the round trip.
    profile = InvestmentProfile(
        user_id="u1",
        created_at="2024-01-01T00:00:00Z",
        risk_tolerance=RiskTolerance.MEDIUM,
        max_drawdown_tolerance_pct=20.0,
        time_horizon_years=10,
        liquidity_needs=LiquidityNeeds(),
        income=IncomeProfile(annual_gross=100_000, stability="stable"),
        net_worth=NetWorth(total=200_000, investable_assets=150_000),
        savings_rate=SavingsRate(monthly=500, annual=6000),
        tax_profile=TaxProfile(country="US"),
        preferences=UserPreferences(),
        constraints=PortfolioConstraints(),
    )
    ips = IPS(profile=profile)

    pd["u1"] = ips
    assert "u1" in pd
    assert pd.get("u1") is not None

    # __getitem__ returns the stored data dict.
    fetched = pd["u1"]
    assert fetched["profile"]["user_id"] == "u1"

    # Overwrite via __setitem__ — always goes through create_job's atomic
    # upsert now (see test_persistent_dict_setitem_always_upserts_no_read).
    pd["u1"] = ips
    assert pd.get("u1") is not None

    # values() returns a list of dicts.
    vals = pd.values()
    assert len(vals) == 1
    assert vals[0]["profile"]["user_id"] == "u1"

    # pop with default — missing key returns default.
    assert pd.pop("missing", "DEFAULT") == "DEFAULT"
    # pop existing returns the value.
    popped = pd.pop("u1", "FALLBACK")
    assert popped["profile"]["user_id"] == "u1"
    assert "u1" not in pd

    # KeyError on bare __getitem__ for missing key.
    with pytest.raises(KeyError):
        _ = pd["nope"]
    # pop without default and missing key raises KeyError.
    with pytest.raises(KeyError):
        pd.pop("nope")

    # Storing a non-Pydantic value goes through the {"value": ...} path.
    pd["plain"] = 42
    assert pd.get("plain") == {"value": 42}

    # __delitem__ removes silently.
    del pd["plain"]
    assert pd.get("plain") is None

    # get() with default returns the default when key missing.
    assert pd.get("missing", "SENTINEL") == "SENTINEL"


def test_persistent_dict_pop_rejects_extra_positional_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pop() must reject a second default argument with TypeError, matching
    dict.pop's contract, instead of silently returning the first one."""
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict

    pd = _PersistentDict("pop_arity_test")

    with pytest.raises(TypeError):
        pd.pop("missing", "a", "b")


def test_persistent_dict_pop_accepts_explicit_none_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit default=None must be distinguishable from "no default
    passed" -- pop() should return None, not raise KeyError."""
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict

    pd = _PersistentDict("pop_none_default_test")

    assert pd.pop("missing", None) is None


class _HasNonCallableModelDumpAttr:
    """A lookalike object exposing a non-Pydantic, non-callable ``model_dump``
    attribute -- the old ``hasattr(value, "model_dump")`` check would try to
    call this and raise ``TypeError``; ``isinstance(value, BaseModel)`` must
    not."""

    model_dump = "not a method"


def test_persistent_dict_setitem_treats_non_basemodel_model_dump_attr_as_plain_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict

    pd = _PersistentDict("model_dump_lookalike_test")
    client: _FakeJobClient = pd._client  # type: ignore[assignment]

    obj = _HasNonCallableModelDumpAttr()
    pd["k"] = obj  # must not raise TypeError trying to call the attribute

    assert client._jobs["k"]["data"] == {"value": obj}


def test_persistent_dict_init_delegates_to_get_job_service_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__init__ must resolve its client via the shared get_job_service_client
    factory rather than constructing a JobServiceClient directly."""
    import job_service_client as jsc_mod
    from investment_team.api.main import _PersistentDict

    calls: List[str] = []
    sentinel_client = _FakeJobClient(team="spy")

    def _spy_get_job_service_client(team: str):
        calls.append(team)
        return sentinel_client

    monkeypatch.setattr(jsc_mod, "get_job_service_client", _spy_get_job_service_client)

    pd = _PersistentDict("spy_test")

    assert calls == ["investment_spy_test"]
    assert pd._client is sentinel_client


def test_persistent_dict_init_shares_cached_client_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two _PersistentDict instances for the same entity_type must share one
    process-wide cached JobServiceClient instead of each opening their own."""
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict

    pd_a = _PersistentDict("shared_test")
    pd_b = _PersistentDict("shared_test")

    assert pd_a._client is pd_b._client


class _LostDeleteRaceClient:
    """Stub JobServiceClient simulating a lost pop() race: get_job still
    finds the job (read before the race is settled), but delete_job reports
    no row was removed -- a concurrent pop() for the same key already won.
    """

    def __init__(self, job: Dict[str, Any], team: str = "x", base_url: str | None = None) -> None:
        self._job = job

    def get_job(self, job_id: str):
        return dict(self._job) if job_id == self._job["job_id"] else None

    def delete_job(self, job_id: str) -> bool:
        return False


def test_persistent_dict_pop_treats_lost_delete_race_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If delete_job reports no row was actually removed (a concurrent pop()
    for the same key already won that race), this call must not hand back
    the data it read moments earlier as though it had exclusively popped it
    -- issue #4253."""
    import job_service_client as jsc_mod
    from investment_team.api.main import _PersistentDict

    job = {"job_id": "k", "status": "stored", "data": {"value": "x"}}
    monkeypatch.setattr(
        jsc_mod, "JobServiceClient", lambda **kwargs: _LostDeleteRaceClient(job, **kwargs)
    )

    pd = _PersistentDict("race_test")

    assert pd.pop("k", "DEFAULT") == "DEFAULT"
    with pytest.raises(KeyError):
        pd.pop("k")


def test_persistent_dict_setitem_always_upserts_no_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """__setitem__ must go straight to create_job's atomic upsert, with no
    get_job read-before-write -- that read was the check-then-act race this
    fix removes (issue #4213)."""
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict

    pd = _PersistentDict("upsert_test")
    client: _FakeJobClient = pd._client  # type: ignore[assignment]

    pd["k"] = "first"
    assert client.get_job_calls == 0
    assert client.create_job_calls == 1
    assert client.update_job_calls == 0
    # Inspect the fake store directly rather than via pd.get(), which itself
    # calls get_job and would confound the call-count assertions below.
    assert client._jobs["k"]["data"] == {"value": "first"}

    # Overwriting an existing key still never reads first, and still goes
    # through create_job (the DB-layer ON CONFLICT DO UPDATE), not update_job.
    pd["k"] = "second"
    assert client.get_job_calls == 0
    assert client.create_job_calls == 2
    assert client.update_job_calls == 0
    assert client._jobs["k"]["data"] == {"value": "second"}


def test_persistent_dict_setitem_concurrent_writes_same_key_no_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many threads writing the same key concurrently must never raise or
    leave the store split/corrupted -- regression guard for the
    check-then-act race described in issue #4213 (two writers both
    observing "no existing job" and both calling create_job)."""
    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _FakeJobClient)
    from investment_team.api.main import _PersistentDict

    pd = _PersistentDict("concurrent_test")
    errors: List[BaseException] = []

    def _write(i: int) -> None:
        try:
            pd["shared-key"] = f"value-{i}"
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors
    stored = pd.get("shared-key")
    assert stored is not None
    assert stored["value"].startswith("value-")


def test_persistent_dict_values_return_annotation() -> None:
    """_PersistentDict.values must advertise List[Any] for static analysis."""
    from typing import get_type_hints

    from investment_team.api.main import _PersistentDict

    hints = get_type_hints(_PersistentDict.values)
    assert hints["return"] == List[Any]


# ---------------------------------------------------------------------------
# Lazy agent singleton factories (_get_advisor_agent / _get_policy_guardian /
# _get_orchestrator / _get_committee_agent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory_name",
    ["_get_advisor_agent", "_get_policy_guardian", "_get_orchestrator", "_get_committee_agent"],
)
def test_agent_factory_returns_cached_singleton_until_cleared(factory_name: str) -> None:
    """Each lazy factory must return the same cached instance across repeated
    calls, and a genuinely new instance after cache_clear() -- proving it's a
    real lazy/rebuildable singleton, not a disguised eager one."""
    from investment_team.api import main as api_main

    factory = getattr(api_main, factory_name)

    first = factory()
    second = factory()
    assert first is second

    factory.cache_clear()
    third = factory()
    assert third is not first


# ---------------------------------------------------------------------------
# _run_backtest_background — direct invocation with stubbed dependencies
# ---------------------------------------------------------------------------


def test_run_backtest_background_completes(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestResult,
        StrategySpec,
    )

    # Stub the job-store helpers (instead of patching the real job service).
    state: Dict[str, Any] = {}

    def _fake_update_if_not_cancelled(jid, **kw):
        state.update(kw)
        return True

    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)
    monkeypatch.setattr(
        api_main,
        "_bt_update_job_if_not_cancelled",
        _fake_update_if_not_cancelled,
    )

    bt_result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    def _fake_run(strategy, config):
        return bt_result, []

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _fake_run)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )

    status = api_main._run_backtest_background("job-1", strategy, config, "tester", [])
    # Final state update is to COMPLETED.
    assert state.get("status") == "completed"
    assert state.get("backtest_id", "").startswith("bt-")
    assert status == api_main._BT_JOB_STATUS_COMPLETED


def test_run_backtest_background_handles_investment_backtest_error(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.exceptions import StrategyExecutionError
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    monkeypatch.setattr(
        api_main, "_bt_update_job_if_not_cancelled", lambda jid, **kw: state.update(kw) or True
    )

    def _raises_domain_error(strategy, config):
        raise StrategyExecutionError("bad strategy")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _raises_domain_error)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )
    status = api_main._run_backtest_background("job-2", strategy, config, "tester", None)
    assert state.get("status") == "failed"
    assert state.get("error") == "bad strategy"
    assert status == api_main._BT_JOB_STATUS_FAILED


def test_run_backtest_background_handles_generic_exception(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    monkeypatch.setattr(
        api_main, "_bt_update_job_if_not_cancelled", lambda jid, **kw: state.update(kw) or True
    )

    def _raises_generic(strategy, config):
        raise RuntimeError("network down")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _raises_generic)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )
    status = api_main._run_backtest_background("job-3", strategy, config, "tester", None)
    assert state.get("status") == "failed"
    assert "network down" in (state.get("error") or "")
    assert status == api_main._BT_JOB_STATUS_FAILED


def test_run_backtest_background_early_cancellation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    # RUNNING write is rejected as if a cancel landed before it — no state write.
    monkeypatch.setattr(api_main, "_bt_update_job_if_not_cancelled", lambda jid, **kw: False)

    def _should_not_run(strategy, config):
        raise AssertionError("backtest must not run when cancelled")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _should_not_run)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )
    status = api_main._run_backtest_background("job-4", strategy, config, "tester", None)
    # No update calls — early return.
    assert state == {}
    assert status == api_main._BT_JOB_STATUS_CANCELLED


def test_bt_terminal_status_for_write_tri_state(api_client) -> None:
    """True -> continue (None); False -> cancelled; None (row gone) -> missing."""
    from investment_team.api import main as api_main

    assert api_main._bt_terminal_status_for_write(True) is None
    assert api_main._bt_terminal_status_for_write(False) == api_main._BT_JOB_STATUS_CANCELLED
    assert api_main._bt_terminal_status_for_write(None) == api_main._BT_JOB_STATUS_MISSING


def test_run_backtest_background_job_missing_before_start(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """RUNNING write reports the row is gone (e.g. deleted via DELETE /backtests/jobs/{id})
    before the backtest ever starts — must be reported as missing, not cancelled."""
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    # Row already deleted: `update_job_if_not_cancelled` returns None, not False.
    monkeypatch.setattr(api_main, "_bt_update_job_if_not_cancelled", lambda jid, **kw: None)

    def _should_not_run(strategy, config):
        raise AssertionError("backtest must not run when the job row is gone")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _should_not_run)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )
    status = api_main._run_backtest_background("job-missing", strategy, config, "tester", None)
    assert status == api_main._BT_JOB_STATUS_MISSING


def test_run_backtest_background_job_deleted_mid_run(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The COMPLETED write races a concurrent DELETE that removed the job row
    after the backtest ran to completion — must be reported as missing, not
    cancelled (no cancellation actually happened)."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestResult,
        StrategySpec,
    )

    calls: List[Dict[str, Any]] = []

    def _fake_update_if_not_cancelled(jid, **kw):
        calls.append(kw)
        # RUNNING write succeeds; COMPLETED write finds the row gone.
        return True if kw.get("status") == api_main._BT_JOB_STATUS_RUNNING else None

    monkeypatch.setattr(api_main, "_bt_update_job_if_not_cancelled", _fake_update_if_not_cancelled)
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)

    bt_result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    monkeypatch.setattr(
        api_main, "_run_real_data_backtest", lambda strategy, config: (bt_result, [])
    )

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )
    status = api_main._run_backtest_background(
        "job-deleted-mid-run", strategy, config, "tester", []
    )
    assert status == api_main._BT_JOB_STATUS_MISSING
    assert [c.get("status") for c in calls] == [
        api_main._BT_JOB_STATUS_RUNNING,
        api_main._BT_JOB_STATUS_COMPLETED,
    ]


def test_run_backtest_background_mid_run_cancellation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestResult,
        StrategySpec,
    )

    state: Dict[str, Any] = {}
    # RUNNING write succeeds; the single remaining mid-run cancel check (after
    # the backtest executes, before the COMPLETED write) reports cancelled.
    monkeypatch.setattr(
        api_main, "_bt_update_job_if_not_cancelled", lambda jid, **kw: state.update(kw) or True
    )
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: True)

    bt_result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    monkeypatch.setattr(
        api_main, "_run_real_data_backtest", lambda strategy, config: (bt_result, [])
    )

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )

    status = api_main._run_backtest_background("job-5", strategy, config, "tester", None)
    assert status == api_main._BT_JOB_STATUS_CANCELLED
    assert state.get("status") == "running"
    assert "backtest_id" not in state


def test_run_backtest_background_cancel_at_completed_write(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A cancel landing after the backtest record is already persisted but
    before/during the atomic COMPLETED write must still be reported as
    cancelled — the write itself, not a separate prior check, is what catches
    it."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestResult,
        StrategySpec,
    )

    state: Dict[str, Any] = {}
    # Mid-run check (right after the backtest executes) is not yet cancelled;
    # the RUNNING write succeeds; the COMPLETED write is the one that finds
    # the job cancelled.
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)

    def _fake_update_if_not_cancelled(jid, **kw):
        state.update(kw)
        return kw.get("status") != api_main._BT_JOB_STATUS_COMPLETED

    monkeypatch.setattr(api_main, "_bt_update_job_if_not_cancelled", _fake_update_if_not_cancelled)

    bt_result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    monkeypatch.setattr(
        api_main, "_run_real_data_backtest", lambda strategy, config: (bt_result, [])
    )

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )

    status = api_main._run_backtest_background(
        "job-cancel-at-completed", strategy, config, "tester", []
    )

    assert status == api_main._BT_JOB_STATUS_CANCELLED
    assert state.get("status") == api_main._BT_JOB_STATUS_COMPLETED
    # The backtest record was already persisted before the guarded write
    # rejected the COMPLETED status — the guard skips the status write, not
    # the record write.
    assert len(api_main._backtests) == 1


def test_run_backtest_background_cancel_during_backtest_execution_error(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    # First write (RUNNING) succeeds; second write (FAILED, from the except
    # block) is rejected as if a cancel landed during backtest execution.
    update_results = iter([True, False])

    def _fake_update_if_not_cancelled(jid, **kw):
        ok = next(update_results)
        if ok:
            state.update(kw)
        return ok

    monkeypatch.setattr(api_main, "_bt_update_job_if_not_cancelled", _fake_update_if_not_cancelled)

    def _raises_backtest_error(strategy, config):
        raise api_main.BacktestExecutionError(status_code=422, detail="bad strategy")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _raises_backtest_error)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )

    status = api_main._run_backtest_background("job-6", strategy, config, "tester", None)
    assert status == api_main._BT_JOB_STATUS_CANCELLED
    assert state.get("status") == "running"
    assert "error" not in state


def test_run_backtest_background_cancel_during_generic_exception(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    # First write (RUNNING) succeeds; second write (FAILED, from the except
    # block) is rejected as if a cancel landed during backtest execution.
    update_results = iter([True, False])

    def _fake_update_if_not_cancelled(jid, **kw):
        ok = next(update_results)
        if ok:
            state.update(kw)
        return ok

    monkeypatch.setattr(api_main, "_bt_update_job_if_not_cancelled", _fake_update_if_not_cancelled)

    def _raises_generic(strategy, config):
        raise RuntimeError("network down")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _raises_generic)

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )

    status = api_main._run_backtest_background("job-7", strategy, config, "tester", None)
    assert status == api_main._BT_JOB_STATUS_CANCELLED
    assert state.get("status") == "running"
    assert "error" not in state


def test_run_backtest_background_retry_reuses_backtest_id(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A second run for the same job_id (e.g. a Temporal activity retry after a
    worker crash left the job RUNNING) must overwrite the same backtest record
    instead of minting a duplicate — the defect reported for retries."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestResult,
        StrategySpec,
    )

    state: Dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)
    monkeypatch.setattr(
        api_main, "_bt_update_job_if_not_cancelled", lambda jid, **kw: state.update(kw) or True
    )

    bt_result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    monkeypatch.setattr(
        api_main, "_run_real_data_backtest", lambda strategy, config: (bt_result, [])
    )

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    config = BacktestConfig(
        start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
    )

    status1 = api_main._run_backtest_background("job-retry", strategy, config, "tester", [])
    first_backtest_id = state["backtest_id"]

    status2 = api_main._run_backtest_background("job-retry", strategy, config, "tester", [])
    second_backtest_id = state["backtest_id"]

    assert first_backtest_id == second_backtest_id
    # The second run overwrote the same record rather than adding a duplicate.
    assert len(api_main._backtests.values()) == 1
    assert status1 == api_main._BT_JOB_STATUS_COMPLETED
    assert status2 == api_main._BT_JOB_STATUS_COMPLETED


# ---------------------------------------------------------------------------
# _purge_strategy_lab_job_storage + _delete_paper_sessions_for_lab_record
# ---------------------------------------------------------------------------


def test_delete_paper_sessions_for_lab_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only sessions referencing the lab_record_id are deleted."""
    import job_service_client as jsc_mod

    fake = _FakeJobClient(team="investment_paper_trading_sessions")
    fake.create_job("pt-1", data={"lab_record_id": "lab-1"})
    fake.create_job("pt-2", data={"lab_record_id": "lab-other"})
    fake.create_job("pt-3", data={"lab_record_id": "lab-1"})
    fake.create_job("pt-4", data="not-a-dict")
    fake.create_job("pt-5")  # record with no lab_record_id data — should not match
    monkeypatch.setattr(jsc_mod, "JobServiceClient", lambda team=None: fake)

    from investment_team.api.main import _delete_paper_sessions_for_lab_record

    deleted = _delete_paper_sessions_for_lab_record("lab-1")
    assert deleted == 2


def test_purge_strategy_lab_job_storage_filters_by_id_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strategies / backtests are only deleted when their ID has the lab prefix."""
    import job_service_client as jsc_mod

    clients_by_team: Dict[str, _FakeJobClient] = {}

    def _factory(team: str = "x"):
        if team not in clients_by_team:
            clients_by_team[team] = _FakeJobClient(team=team)
        return clients_by_team[team]

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _factory)

    lab = _factory("investment_strategy_lab_records")
    lab.create_job("lab-1")
    lab.create_job("lab-2")

    strat = _factory("investment_strategies")
    strat.create_job("strat-lab-A")
    strat.create_job("strat-non-lab-B")

    bt = _factory("investment_backtests")
    bt.create_job("bt-lab-A")
    bt.create_job("bt-non-lab-B")

    paper = _factory("investment_paper_trading_sessions")
    paper.create_job("pt-1")

    from investment_team.api.main import _purge_strategy_lab_job_storage

    counts = _purge_strategy_lab_job_storage()
    assert counts == {
        "deleted_lab_records": 2,
        "deleted_lab_strategies": 1,
        "deleted_lab_backtests": 1,
        "deleted_paper_trading_sessions": 1,
    }


def test_delete_paper_sessions_for_lab_record_many_jobs_concurrent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrency preserves counts: every matching session is counted exactly once."""
    import job_service_client as jsc_mod

    fake = _FakeJobClient(team="investment_paper_trading_sessions")
    matching = 50
    for i in range(matching):
        fake.create_job(f"pt-match-{i}", data={"lab_record_id": "lab-1"})
    for i in range(20):
        fake.create_job(f"pt-other-{i}", data={"lab_record_id": "lab-other"})
    monkeypatch.setattr(jsc_mod, "JobServiceClient", lambda team=None: fake)

    from investment_team.api.main import _delete_paper_sessions_for_lab_record

    deleted = _delete_paper_sessions_for_lab_record("lab-1")
    assert deleted == matching
    # Only the matching jobs were removed; the others survive.
    remaining = {j["job_id"] for j in fake.list_jobs()}
    assert remaining == {f"pt-other-{i}" for i in range(20)}


def test_delete_paper_sessions_list_jobs_http_error_raises_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job-service transport failure while listing sessions must surface as 503
    (fail closed) — not a bare exception and not a silent skip."""
    import job_service_client as jsc_mod

    class _BrokenListClient(_FakeJobClient):
        def list_jobs(self, *, statuses=None):
            raise httpx.ConnectError("job service unreachable")

        def delete_job(self, job_id: str) -> bool:
            raise AssertionError("delete_job must not run when list_jobs fails")

    monkeypatch.setattr(
        jsc_mod, "JobServiceClient", lambda team=None: _BrokenListClient(team=team or "x")
    )

    from investment_team.api.main import _delete_paper_sessions_for_lab_record

    with pytest.raises(HTTPException) as ei:
        _delete_paper_sessions_for_lab_record("lab-1")
    assert ei.value.status_code == 503
    assert "temporarily unavailable" in str(ei.value.detail).lower()


def test_delete_paper_sessions_list_jobs_runtime_error_raises_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unconfigured JOB_SERVICE_URL (RuntimeError from JobServiceClient.__init__)
    must also surface as 503 so delete_strategy_lab_record can leave state intact."""
    import job_service_client as jsc_mod

    def _unconfigured_factory(team=None):
        raise RuntimeError("JOB_SERVICE_URL is not configured")

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _unconfigured_factory)

    from investment_team.api.main import _delete_paper_sessions_for_lab_record

    with pytest.raises(HTTPException) as ei:
        _delete_paper_sessions_for_lab_record("lab-1")
    assert ei.value.status_code == 503
    assert "temporarily unavailable" in str(ei.value.detail).lower()


def test_purge_strategy_lab_job_storage_many_jobs_concurrent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent fan-out across all four teams returns exact per-team counts."""
    import job_service_client as jsc_mod

    clients_by_team: Dict[str, _FakeJobClient] = {}

    def _factory(team: str = "x"):
        if team not in clients_by_team:
            clients_by_team[team] = _FakeJobClient(team=team)
        return clients_by_team[team]

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _factory)

    lab = _factory("investment_strategy_lab_records")
    for i in range(30):
        lab.create_job(f"lab-{i}")

    strat = _factory("investment_strategies")
    for i in range(25):
        strat.create_job(f"strat-lab-{i}")
    for i in range(10):
        strat.create_job(f"strat-keep-{i}")

    bt = _factory("investment_backtests")
    for i in range(15):
        bt.create_job(f"bt-lab-{i}")
    for i in range(7):
        bt.create_job(f"bt-keep-{i}")

    paper = _factory("investment_paper_trading_sessions")
    for i in range(40):
        paper.create_job(f"pt-{i}")

    from investment_team.api.main import _purge_strategy_lab_job_storage

    counts = _purge_strategy_lab_job_storage()
    assert counts == {
        "deleted_lab_records": 30,
        "deleted_lab_strategies": 25,
        "deleted_lab_backtests": 15,
        "deleted_paper_trading_sessions": 40,
    }
    # Non-lab strategies/backtests are untouched.
    assert {j["job_id"] for j in strat.list_jobs()} == {f"strat-keep-{i}" for i in range(10)}
    assert {j["job_id"] for j in bt.list_jobs()} == {f"bt-keep-{i}" for i in range(7)}


def test_purge_strategy_lab_job_storage_reports_none_for_timed_out_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unit that doesn't finish within the shared deadline is reported as
    None (unknown, still in flight) rather than a misleadingly-confirmed 0."""
    import job_service_client as jsc_mod
    from investment_team.strategy_lab import orchestrator_api

    # Timeout lives (and is read) on orchestrator_api; patching api.main's
    # re-export alias would not shrink the deadline the purge helper uses.
    monkeypatch.setattr(orchestrator_api, "_PURGE_TIMEOUT_S", 0.2)

    release = threading.Event()

    class _SlowLabRecordsClient(_FakeJobClient):
        """Blocks list_jobs past the (shrunk) shared deadline for one team only,
        so its unit is still "in flight" when the collector's deadline elapses."""

        def list_jobs(self, *, statuses=None):
            if self.team == "investment_strategy_lab_records":
                assert release.wait(timeout=5.0), "test never released the slow unit"
            return super().list_jobs(statuses=statuses)

    clients_by_team: Dict[str, _SlowLabRecordsClient] = {}

    def _factory(team: str = "x"):
        if team not in clients_by_team:
            clients_by_team[team] = _SlowLabRecordsClient(team=team)
        return clients_by_team[team]

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _factory)

    try:
        counts = orchestrator_api._purge_strategy_lab_job_storage()
    finally:
        # Unblock the slow unit's background thread regardless of outcome, so
        # it doesn't keep running past the end of the test.
        release.set()

    assert counts["deleted_lab_records"] is None
    assert counts["deleted_lab_strategies"] == 0
    assert counts["deleted_lab_backtests"] == 0
    assert counts["deleted_paper_trading_sessions"] == 0


def test_clear_strategy_lab_storage_route(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    """The DELETE /strategy-lab/storage route forwards purge counts."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_purge_strategy_lab_job_storage",
        lambda: {
            "deleted_lab_records": 3,
            "deleted_lab_strategies": 2,
            "deleted_lab_backtests": 1,
            "deleted_paper_trading_sessions": 4,
        },
    )

    resp = api_client.delete("/strategy-lab/storage")
    body = resp.json()
    assert body["deleted_lab_records"] == 3
    assert body["deleted_lab_strategies"] == 2
    assert body["deleted_lab_backtests"] == 1
    assert body["deleted_paper_trading_sessions"] == 4


def test_clear_strategy_lab_storage_does_not_block_on_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The purge runs without holding `_lock`, so a concurrent holder of `_lock`
    (e.g. an in-flight resume/restart transition) doesn't block this call.

    Calls the endpoint function directly (not through ``api_client``) and
    bounds the wait with ``thread.join(timeout=...)``, mirroring the
    established pattern in ``test_restart_strategy_lab_run_serializes_
    concurrent_restarts_for_same_run_id`` — a real deadlock here would
    otherwise hang the test indefinitely instead of failing cleanly.
    """
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_purge_strategy_lab_job_storage",
        lambda: {
            "deleted_lab_records": 1,
            "deleted_lab_strategies": 0,
            "deleted_lab_backtests": 0,
            "deleted_paper_trading_sessions": 0,
        },
    )

    result: List[Any] = []

    def _call() -> None:
        try:
            result.append(api_main.clear_strategy_lab_storage())
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            result.append(exc)

    api_main._lock.acquire()
    try:
        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "clear_strategy_lab_storage blocked while _lock was held"
    finally:
        api_main._lock.release()

    assert len(result) == 1
    assert not isinstance(result[0], BaseException), result[0]
    assert result[0].deleted_lab_records == 1


# ---------------------------------------------------------------------------
# delete_strategy_lab_record happy path
# ---------------------------------------------------------------------------


def test_delete_strategy_lab_record_success(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id="strat-lab-X",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-lab-X",
        strategy_id="strat-lab-X",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    record = StrategyLabRecord(
        lab_record_id="lab-X",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    api_main._strategy_lab_records["lab-X"] = record
    api_main._strategies["strat-lab-X"] = strat
    api_main._backtests["bt-lab-X"] = bt

    # Stub the side-effecting paper-session cleanup so we don't need a JobService.
    monkeypatch.setattr(api_main, "_delete_paper_sessions_for_lab_record", lambda lab_id: 2)

    resp = api_client.delete("/strategy-lab/records/lab-X")
    body = resp.json()
    assert body["lab_record_id"] == "lab-X"
    assert body["deleted_strategy_id"] == "strat-lab-X"
    assert body["deleted_backtest_id"] == "bt-lab-X"
    assert body["deleted_paper_trading_sessions"] == 2
    # Underlying stores were cleaned up.
    assert api_main._strategy_lab_records.get("lab-X") is None
    assert api_main._strategies.get("strat-lab-X") is None
    assert api_main._backtests.get("bt-lab-X") is None


def test_delete_strategy_lab_record_deletes_job_service_rows(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The linked strategy/backtest deletes must reach the job service, not
    just an in-memory cache.

    ``api_client`` normally swaps ``_strategies``/``_backtests`` for a plain
    ``_InMemoryDict``, so ``test_delete_strategy_lab_record_success`` alone
    can't tell a real ``JobServiceClient.delete_job`` call apart from a
    no-op. This test wires ``_strategies``/``_backtests`` back to real
    ``_PersistentDict`` instances backed by fake job-service clients, so a
    regression to "only clears the in-memory entry" would leave the fake
    clients' rows in place and fail the assertions below.
    """
    import job_service_client as jsc_mod
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    fake_strategies_client = _FakeJobClient(team="investment_strategies")
    fake_backtests_client = _FakeJobClient(team="investment_backtests")
    monkeypatch.setitem(jsc_mod._client_cache, "investment_strategies", fake_strategies_client)
    monkeypatch.setitem(jsc_mod._client_cache, "investment_backtests", fake_backtests_client)
    monkeypatch.setattr(api_main, "_strategies", api_main._PersistentDict("strategies"))
    monkeypatch.setattr(api_main, "_backtests", api_main._PersistentDict("backtests"))

    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id="strat-lab-Z",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-lab-Z",
        strategy_id="strat-lab-Z",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    record = StrategyLabRecord(
        lab_record_id="lab-Z",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    api_main._strategy_lab_records["lab-Z"] = record
    api_main._strategies["strat-lab-Z"] = strat
    api_main._backtests["bt-lab-Z"] = bt

    monkeypatch.setattr(api_main, "_delete_paper_sessions_for_lab_record", lambda lab_id: 0)

    resp = api_client.delete("/strategy-lab/records/lab-Z")
    body = resp.json()
    assert body["deleted_strategy_id"] == "strat-lab-Z"
    assert body["deleted_backtest_id"] == "bt-lab-Z"
    # The regression this test guards against: the rows must be gone from
    # the fake job-service clients, not merely absent from a local dict.
    assert fake_strategies_client.get_job("strat-lab-Z") is None
    assert fake_backtests_client.get_job("bt-lab-Z") is None


def test_delete_strategy_lab_record_reports_none_for_missing_strategy_and_backtest(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """When the linked strategy/backtest are already absent from their stores,
    the response must not claim they were deleted."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id="strat-lab-Y",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-lab-Y",
        strategy_id="strat-lab-Y",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    record = StrategyLabRecord(
        lab_record_id="lab-Y",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    api_main._strategy_lab_records["lab-Y"] = record
    # Deliberately do NOT seed _strategies/_backtests, simulating a lab record
    # whose linked strategy/backtest were already removed by an earlier call.

    monkeypatch.setattr(api_main, "_delete_paper_sessions_for_lab_record", lambda lab_id: 0)

    resp = api_client.delete("/strategy-lab/records/lab-Y")
    body = resp.json()
    assert body["lab_record_id"] == "lab-Y"
    assert body["deleted_strategy_id"] is None
    assert body["deleted_backtest_id"] is None
    assert body["deleted_paper_trading_sessions"] == 0
    assert api_main._strategy_lab_records.get("lab-Y") is None


def test_delete_strategy_lab_record_preserves_record_when_paper_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """If paper-session cleanup fails, the lab record/strategy/backtest must NOT
    be deleted -- a retry should re-attempt cleanup, not 404 against an
    already-deleted record while paper sessions sit orphaned in the job service."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id="strat-lab-Z",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-lab-Z",
        strategy_id="strat-lab-Z",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    record = StrategyLabRecord(
        lab_record_id="lab-Z",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    api_main._strategy_lab_records["lab-Z"] = record
    api_main._strategies["strat-lab-Z"] = strat
    api_main._backtests["bt-lab-Z"] = bt

    def _broken_cleanup(lab_id: str) -> int:
        raise RuntimeError("job service unreachable")

    monkeypatch.setattr(api_main, "_delete_paper_sessions_for_lab_record", _broken_cleanup)

    with pytest.raises(RuntimeError, match="job service unreachable"):
        api_main.delete_strategy_lab_record("lab-Z")

    # Nothing was deleted: the record stays retryable instead of becoming an
    # orphan-producing 404.
    assert api_main._strategy_lab_records.get("lab-Z") is not None
    assert api_main._strategies.get("strat-lab-Z") is not None
    assert api_main._backtests.get("bt-lab-Z") is not None


def test_delete_strategy_lab_record_preserves_record_when_list_jobs_fails(
    monkeypatch: pytest.MonkeyPatch,
    api_client,
) -> None:
    """list_jobs transport failure must yield 503 and leave lab state intact
    so a retry can re-attempt paper-session cleanup."""
    import job_service_client as jsc_mod
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id="strat-lab-Y",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-lab-Y",
        strategy_id="strat-lab-Y",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    record = StrategyLabRecord(
        lab_record_id="lab-Y",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    api_main._strategy_lab_records["lab-Y"] = record
    api_main._strategies["strat-lab-Y"] = strat
    api_main._backtests["bt-lab-Y"] = bt

    class _BrokenListClient(_FakeJobClient):
        def list_jobs(self, *, statuses=None):
            raise httpx.ConnectError("job service unreachable")

    # Patch only after seeding so PersistentDict writes during setup succeed.
    monkeypatch.setattr(
        jsc_mod, "JobServiceClient", lambda team=None: _BrokenListClient(team=team or "x")
    )

    with pytest.raises(HTTPException) as ei:
        api_main.delete_strategy_lab_record("lab-Y")
    assert ei.value.status_code == 503

    assert api_main._strategy_lab_records.get("lab-Y") is not None
    assert api_main._strategies.get("strat-lab-Y") is not None
    assert api_main._backtests.get("bt-lab-Y") is not None


# ---------------------------------------------------------------------------
# _recover_orphaned_paper_trading_sessions startup hook
# ---------------------------------------------------------------------------


def test_recover_orphaned_paper_trading_sessions_marks_running_as_failed(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
        StrategySpec,
    )

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    session_active = PaperTradingSession(
        session_id="pt-active",
        lab_record_id="lab-1",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=["X"],
        data_source="yahoo",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
    )
    session_done = PaperTradingSession(
        session_id="pt-done",
        lab_record_id="lab-1",
        strategy=strategy,
        status=PaperTradingStatus.COMPLETED,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=["X"],
        data_source="yahoo",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
        completed_at="2024-06-01T01:00:00Z",
    )
    api_main._paper_trading_sessions["pt-active"] = session_active
    api_main._paper_trading_sessions["pt-done"] = session_done

    api_main._recover_orphaned_paper_trading_sessions()

    recovered = api_main._paper_trading_sessions.get("pt-active")
    assert recovered.status == PaperTradingStatus.FAILED
    assert recovered.terminated_reason == "process_exit"

    # The already-completed session is untouched.
    untouched = api_main._paper_trading_sessions.get("pt-done")
    assert untouched.status == PaperTradingStatus.COMPLETED


# ---------------------------------------------------------------------------
# _load_run_from_job_service + _persist_run_state
# ---------------------------------------------------------------------------


def test_load_run_from_job_service_propagates_job_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine job-service failure (transport error, 5xx, ...) must propagate
    rather than be silently remapped to "not found" -- swallowing it would let
    a resumed run silently restart from offset 0 instead of failing closed."""
    from investment_team.strategy_lab import run_state

    class _Broken:
        def get_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Broken())
    with pytest.raises(RuntimeError, match="backend down"):
        run_state.load_run_from_job_service("run-x")


def test_load_run_from_job_service_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely missing job (the job service returns job=None, not an error)
    still cleanly returns None -- the fix above must not regress this path."""
    from investment_team.strategy_lab import run_state

    class _Empty:
        def get_job(self, *a, **k):
            return None

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Empty())
    assert run_state.load_run_from_job_service("run-x") is None


def test_load_run_from_job_service_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "completed", "data": {"foo": 1}}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    out = run_state.load_run_from_job_service("run-y")
    assert out is not None
    assert out["foo"] == 1
    assert out["run_id"] == "run-y"
    assert out["status"] == "completed"


def test_get_run_state_strict_propagates_durable_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: unlike the lenient get_run_state (whose durable fallback
    swallows ANY job-service read failure via load_run_from_job_service),
    get_run_state_strict must propagate a transport failure rather than
    returning None -- a caller relying on this to distinguish "no prior
    state" from "the read failed" needs the raise."""
    from investment_team.strategy_lab import run_state

    class _Broken:
        def get_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Broken())
    with pytest.raises(RuntimeError, match="backend down"):
        run_state.get_run_state_strict("run-strict-fail")


def test_get_run_state_strict_prefers_active_runs_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab import run_state

    monkeypatch.setitem(run_state.active_runs, "run-cached", {"status": "running"})
    try:
        assert run_state.get_run_state_strict("run-cached") == {"status": "running"}
    finally:
        del run_state.active_runs["run-cached"]


def test_get_run_state_strict_returns_none_for_genuinely_unknown_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.strategy_lab import run_state

    class _NotFound:
        def get_job(self, jid):
            return None

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _NotFound())
    assert run_state.get_run_state_strict("run-nonexistent") is None


def test_rehydrate_active_run_offset_propagates_durable_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a transient job-service outage during dispatch must raise
    (letting _dispatch_strategy_lab_run's exception boundary turn it into a
    503 + failed run), not silently return offset 0 -- which would be
    indistinguishable from "fresh run" and cause a resumed run to replay
    already-completed cycles with no error for Temporal to retry on."""
    from investment_team.strategy_lab import run_state

    class _Broken:
        def get_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Broken())
    with pytest.raises(RuntimeError, match="backend down"):
        run_state.rehydrate_active_run_offset("run-offset-fail")


def test_rehydrate_active_run_offset_raises_on_corrupt_contiguous_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a durable-read succeeds but the persisted contiguous_cycles
    field itself is corrupt (unparseable as an int). Silently defaulting to
    offset 0 here would be indistinguishable from a genuinely fresh run and
    cause a resumed run to replay already-completed cycles -- the same
    replay risk this function's docstring already argues against for a
    durable-read failure, so a corrupt field must raise too, not default."""
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "contiguous_cycles": "not-a-number"}

    monkeypatch.setattr(run_state, "active_runs", {})
    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    with pytest.raises(ValueError, match="Invalid persisted contiguous_cycles"):
        run_state.rehydrate_active_run_offset("run-corrupt-offset")


def test_get_resume_seed_counters_propagates_durable_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression as rehydrate_active_run_offset's: a transient
    job-service outage must raise rather than silently reset these counters
    to zero."""
    from investment_team.strategy_lab import run_state

    class _Broken:
        def get_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Broken())
    with pytest.raises(RuntimeError, match="backend down"):
        run_state.get_resume_seed_counters("run-counters-fail")


# ---------------------------------------------------------------------------
# get_run_generation_strict + _build_run_state's generation field (#4029)
#
# get_run_generation_strict is the sole read path for a run's fencing
# generation (there is no lenient sibling -- every other caller already has
# its own known/just-minted value in hand and passes it through explicitly
# rather than re-deriving it via a read; see _dispatch_strategy_lab_run's
# precondition).
# ---------------------------------------------------------------------------


def test_get_run_generation_strict_ignores_active_runs_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: unlike get_run_state's other callers, get_run_generation_strict
    must NOT prefer (or even consult) the process-local active_runs cache. It's
    called from inside a Temporal worker, which may be a different process than
    the API server that handled a restart -- if a stale in-memory generation
    were trusted here, a restart handled elsewhere would never be observed and
    fencing would be silently defeated."""
    from investment_team.strategy_lab import run_state

    monkeypatch.setitem(run_state.active_runs, "run-live", {"generation": 1})  # stale local cache

    class _Ok:
        def get_job(self, jid):
            return {
                "job_id": jid,
                "status": "running",
                "generation": 5,
            }  # authoritative durable value

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    try:
        assert run_state.get_run_generation_strict("run-live") == 5
    finally:
        del run_state.active_runs["run-live"]


def test_get_run_generation_strict_null_data_field_defaults_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a job record with "data": None (key present but null --
    the same shape normalize_persisted_job explicitly guards against) must
    not crash with AttributeError from None.get(...). Falls back to the
    top-level job dict, matching normalize_persisted_job's coercion."""
    from investment_team.strategy_lab import run_state

    class _NullData:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "data": None}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _NullData())
    assert run_state.get_run_generation_strict("run-nulldata") == 1


@pytest.mark.parametrize("empty_value", [None, ""])
def test_get_run_generation_strict_missing_or_empty_value_defaults_to_one(
    monkeypatch: pytest.MonkeyPatch, empty_value
) -> None:
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "generation": empty_value}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    assert run_state.get_run_generation_strict("run-bad") == 1


@pytest.mark.parametrize("nonpositive_value", [0, -1])
def test_get_run_generation_strict_nonpositive_value_clamps_to_one(
    monkeypatch: pytest.MonkeyPatch, nonpositive_value
) -> None:
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "generation": nonpositive_value}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    assert run_state.get_run_generation_strict("run-bad") == 1


@pytest.mark.parametrize("unparseable_value", ["not-a-number", [], {}, object()])
def test_get_run_generation_strict_raises_on_unparseable_value(
    monkeypatch: pytest.MonkeyPatch, unparseable_value
) -> None:
    """Regression: an unparseable persisted `generation` (durable-record
    corruption, not a legitimate missing-field case) must raise rather than
    silently defaulting to the permissive generation 1 -- returning 1 here
    would let a stale pre-restart activity (carrying token 1) pass
    check_fencing_token (which accepts provided_token >= current_token),
    reopening the exact race generation fencing exists to close."""
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "generation": unparseable_value}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    with pytest.raises(ValueError, match="Invalid persisted generation"):
        run_state.get_run_generation_strict("run-bad")


@pytest.mark.parametrize("non_int_value", [2.9, True, False])
def test_get_run_generation_strict_raises_on_float_or_bool(
    monkeypatch: pytest.MonkeyPatch, non_int_value
) -> None:
    """A persisted `generation` that's a float or bool (an int subclass in
    Python, so `isinstance(True, int)` is True) must be rejected as
    corruption rather than silently coerced via int(...) -- a truncated
    float or a bool-derived 0/1 could produce a generation lower than the
    actual persisted value, letting a stale activity pass fencing."""
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "generation": non_int_value}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    with pytest.raises(ValueError, match="Invalid persisted generation"):
        run_state.get_run_generation_strict("run-bad")


def test_get_run_generation_strict_returns_one_for_unknown_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.strategy_lab import run_state

    class _NotFound:
        def get_job(self, jid):
            return None

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _NotFound())
    assert run_state.get_run_generation_strict("run-unknown") == 1


def test_get_run_generation_strict_reads_persisted_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab import run_state

    class _Ok:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running", "generation": 4}

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Ok())
    assert run_state.get_run_generation_strict("run-g4") == 4


def test_get_run_generation_strict_defaults_to_one_when_field_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.strategy_lab import run_state

    class _NoGenerationField:
        def get_job(self, jid):
            return {"job_id": jid, "status": "running"}  # no "generation" key at all

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _NoGenerationField())
    assert run_state.get_run_generation_strict("run-legacy") == 1


def test_get_run_generation_strict_propagates_durable_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a transient durable-read failure must NOT silently default to
    generation 1 -- that would let a stale write past the fencing check during
    exactly the kind of outage fencing needs to guard against. The failure must
    propagate so the caller (the fencing check) rejects the write."""
    from investment_team.strategy_lab import run_state

    class _Broken:
        def get_job(self, jid):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: _Broken())
    with pytest.raises(ConnectionError):
        run_state.get_run_generation_strict("run-x")


def test_build_run_state_generation_defaults_to_one() -> None:
    from investment_team.api import main as api_main

    state = api_main._build_run_state(
        "run-fresh",
        started_at="2024-01-01T00:00:00Z",
        total_cycles=1,
        batch_size=1,
        batch_count=1,
        request_payload={},
    )
    assert state["generation"] == 1


def test_build_run_state_generation_override_is_carried_through() -> None:
    from investment_team.api import main as api_main

    state = api_main._build_run_state(
        "run-restarted",
        started_at="2024-01-01T00:00:00Z",
        total_cycles=1,
        batch_size=1,
        batch_count=1,
        request_payload={},
        generation=7,
    )
    assert state["generation"] == 7


def test_persist_run_state_propagates_job_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine job-service failure must propagate, not be silently logged
    and swallowed -- callers (run/resume/restart dispatch, and the Temporal
    persist activity's retry policy) need to detect a durable-write failure
    instead of continuing as if it succeeded."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import orchestrator_api

    class _Broken:
        def create_job(self, *a, **k):
            raise RuntimeError("backend down")

        def update_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(orchestrator_api, "_get_lab_run_job_client", lambda: _Broken())
    with pytest.raises(RuntimeError, match="backend down"):
        api_main._persist_run_state("run-z", {"status": "running"}, create=True)
    with pytest.raises(RuntimeError, match="backend down"):
        api_main._persist_run_state("run-z", {"status": "running"}, create=False)


def test_persist_run_state_status_less_update_does_not_clobber_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A progress-only update (state without a "status" key -- exactly what
    the Temporal batch workflow's per-cycle/per-batch persists send) must not
    reset the job's status to "running". Previously it defaulted the missing
    status to "running" unconditionally, clobbering a cancelled/failed/
    completed status a concurrent path had already persisted (issue #4185)."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import orchestrator_api

    client = _FakeJobClient()
    client.create_job("run-cancelled", status="cancelled", completed_cycles=2)
    monkeypatch.setattr(orchestrator_api, "_get_lab_run_job_client", lambda: client)

    # A progress-only delta, no "status" key -- must not touch status at all.
    api_main._persist_run_state("run-cancelled", {"completed_cycles": 3}, create=False)

    job = client.get_job("run-cancelled")
    assert job["status"] == "cancelled"
    assert job["completed_cycles"] == 3

    # When state DOES carry a status, it's still written through as before.
    api_main._persist_run_state(
        "run-cancelled", {"status": "failed", "error": "boom"}, create=False
    )
    job = client.get_job("run-cancelled")
    assert job["status"] == "failed"


def test_run_state_to_response_tolerates_non_dict_current_cycle() -> None:
    """A ``current_cycle`` that's present but not a dict (corrupted/foreign
    data) must degrade to ``None`` instead of raising via the ``**cc`` splat."""
    from investment_team.api import main as api_main

    state = {
        "run_id": "run-malformed",
        "status": "running",
        "current_cycle": "not-a-dict",
    }
    response = api_main._run_state_to_response(state)
    assert response.current_cycle is None


def test_run_state_to_response_tolerates_malformed_dict_current_cycle() -> None:
    """A ``current_cycle`` dict that's missing a required field (e.g. ``phase``)
    must degrade to ``None`` instead of raising a Pydantic ValidationError."""
    from investment_team.api import main as api_main

    state = {
        "run_id": "run-malformed-dict",
        "status": "running",
        "current_cycle": {"cycle_index": 1},
    }
    response = api_main._run_state_to_response(state)
    assert response.current_cycle is None


# ---------------------------------------------------------------------------
# run_paper_trading validation branches
# ---------------------------------------------------------------------------


def _winning_record(strategy_code: str | None = "def x(): pass") -> StrategyLabRecord:
    """Build a winning, publishable ``StrategyLabRecord`` with a backing backtest.

    ``strategy_code`` defaults to a trivial snippet; pass ``None`` to build a
    record that fails the "has generated strategy code" validation branch.
    """
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id="strat-w",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code=strategy_code,
    )
    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-w",
        strategy_id="strat-w",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id="lab-w",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        is_publishable=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
        strategy_code=strategy_code,
    )


def test_run_paper_trading_rejects_losing_strategy(api_client) -> None:
    """A lab record with ``is_winning=False`` must be rejected with 400."""
    from investment_team.api import main as api_main

    losing = _winning_record()
    losing.is_winning = False
    losing.is_publishable = False
    api_main._strategy_lab_records["lab-w"] = losing

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 400
    assert "not a winning strategy" in resp.json()["detail"]


def test_run_paper_trading_rejects_non_publishable_strategy(api_client) -> None:
    """A lab record with ``is_publishable=False`` must be rejected with 400,
    and the response detail must surface the skip reason."""
    from investment_team.api import main as api_main

    record = _winning_record()
    record.is_publishable = False
    record.publishability_skip_reason = "realism_failed,alignment_unresolved"
    api_main._strategy_lab_records["lab-w"] = record

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not publishable" in detail
    assert "realism_failed" in detail


def test_run_paper_trading_rejects_when_no_strategy_code(api_client) -> None:
    """A winning, publishable lab record with no generated strategy code must
    still be rejected with 400 (it has nothing executable to paper trade)."""
    from investment_team.api import main as api_main

    record = _winning_record(strategy_code=None)
    api_main._strategy_lab_records["lab-w"] = record

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 400
    assert "no generated strategy code" in resp.json()["detail"]


def test_run_paper_trading_500_on_corrupt_lab_record(api_client) -> None:
    """A persisted record that exists but fails StrategyLabRecord.parse_persisted
    (schema drift, missing required fields, ...) must return a controlled 500
    with a sanitized detail, not an unhandled 500 with a raw pydantic
    traceback leaking internal validation details to the client."""
    from investment_team.api import main as api_main

    # Missing required fields (strategy/backtest/is_winning/...) -- a raw
    # dict that fails StrategyLabRecord's model_validate.
    api_main._strategy_lab_records["lab-corrupt"] = {"lab_record_id": "lab-corrupt"}

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-corrupt"},
    )
    assert resp.status_code == 500
    assert "lab-corrupt" in resp.json()["detail"]
    assert "corrupted" in resp.json()["detail"]


def test_run_paper_trading_kicks_off_background_worker(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy (non-live) path must start a daemon thread and return running."""
    from investment_team.api import main as api_main

    record = _winning_record()
    api_main._strategy_lab_records["lab-w"] = record

    # Replace the background worker so the test doesn't spin up real work.
    started = threading.Event()
    monkeypatch.setattr(api_main, "_run_paper_trading_background", lambda *a, **k: started.set())
    monkeypatch.setattr(api_main, "_live_paper_enabled", lambda: False)

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w", "initial_capital": 50_000.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["status"] in ("running", "opening")
    assert body["session"]["data_source"] == "yahoo_finance"
    # The thread eventually invokes the patched background — wait briefly.
    assert started.wait(timeout=2.0)


# ---------------------------------------------------------------------------
# complete_advisor_session — happy path
# ---------------------------------------------------------------------------


def test_complete_advisor_session_builds_ips(api_client) -> None:
    """Once every required field has been collected via chat replies,
    completing the session must build and return a full IPS for the user."""
    # Start a session, fill all required fields directly, then complete.
    start = api_client.post("/advisor/sessions", json={"user_id": "u-complete"})
    sid = start.json()["session_id"]

    # Fill required fields by sending replies that hit the relevant extractors.
    api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "medium risk"})
    api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "20% drawdown"})
    api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "10 years"})
    api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "120000 stable"})
    api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "500000 350000"})

    # All required fields are now collected. complete should succeed.
    done = api_client.post(f"/advisor/sessions/{sid}/complete")
    assert done.status_code == 200
    body = done.json()
    assert body["user_id"] == "u-complete"
    assert body["ips"]["profile"]["user_id"] == "u-complete"


# ---------------------------------------------------------------------------
# Shutdown hook: event-bus reaper stop + backtest job failure sweep.
# ---------------------------------------------------------------------------


def test_shutdown_hook_marks_running_backtest_jobs_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shutdown hook must sweep any still-RUNNING backtest jobs to FAILED
    and stop the event-bus reaper, so a killed server doesn't leave jobs
    stuck RUNNING forever."""
    from investment_team.api import main as api_main

    calls: List[str] = []
    monkeypatch.setattr(
        api_main, "_bt_mark_all_running_jobs_failed", lambda reason: calls.append(reason)
    )
    monkeypatch.setattr("investment_team.api.job_event_bus.shutdown", lambda: None, raising=False)

    api_main._run_investment_service_shutdown()

    assert calls == ["server shutdown"]


def test_shutdown_hook_swallows_job_store_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising failure-sweep must NOT abort shutdown teardown, and the
    swallowed failure must be logged at WARNING (not DEBUG) so it's visible
    at standard operational log levels."""
    import logging

    from investment_team.api import main as api_main

    def _boom(reason: str) -> None:
        raise RuntimeError("job service unreachable")

    monkeypatch.setattr(api_main, "_bt_mark_all_running_jobs_failed", _boom)
    monkeypatch.setattr("investment_team.api.job_event_bus.shutdown", lambda: None, raising=False)

    with caplog.at_level(logging.WARNING, logger=api_main.logger.name):
        api_main._run_investment_service_shutdown()  # must not raise

    assert any(
        r.levelno == logging.WARNING and "backtest job failure sweep skipped" in r.getMessage()
        for r in caplog.records
    )


def test_shutdown_hook_logs_event_bus_teardown_failure_at_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising event-bus reaper teardown must also be swallowed and logged
    at WARNING, not DEBUG."""
    import logging

    from investment_team.api import main as api_main

    def _boom() -> None:
        raise RuntimeError("event bus unavailable")

    monkeypatch.setattr("investment_team.api.job_event_bus.shutdown", _boom, raising=False)
    monkeypatch.setattr(api_main, "_bt_mark_all_running_jobs_failed", lambda reason: None)

    with caplog.at_level(logging.WARNING, logger=api_main.logger.name):
        api_main._run_investment_service_shutdown()  # must not raise

    assert any(
        r.levelno == logging.WARNING and "event-bus reaper shutdown skipped" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# _finalize_strategy_lab_cycle_record: on_phase callback isolation
# ---------------------------------------------------------------------------


def _make_finalize_test_record(
    lab_record_id: str,
    *,
    is_winning: bool = False,
    is_publishable: bool = False,
    strategy_code: Optional[str] = None,
) -> Any:
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id=f"strat-{lab_record_id}",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=1.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=40.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id=f"bt-{lab_record_id}",
        strategy_id=strat.strategy_id,
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=lab_record_id,
        strategy=strat,
        backtest=bt,
        # is_winning=False takes the earliest skip branch, so the default
        # finalize call only needs to exercise the on_phase callback +
        # persistence — no paper-trading infra required. Callers that need
        # to reach the paper-trading try/except (e.g. is_winning=True,
        # is_publishable=True, strategy_code set) opt in explicitly.
        is_winning=is_winning,
        is_publishable=is_publishable,
        strategy_code=strategy_code,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )


# ---------------------------------------------------------------------------
# _finalize_strategy_lab_cycle_record: per-category signal-brief attribution
#
# ``signal_brief_storage`` covers every allowed asset category in the batch
# (``{"by_asset_class": {<class>: <brief-dict>, ...}}``); a given record was
# pinned to exactly one category, so only that category's entry belongs on
# it. Regression tests: an earlier revision stored the whole multi-category
# map verbatim on every record, which the strategy-card UI cannot render (it
# expects one flat brief, one row per field) and which misattributed every
# other category's brief to a record that was never about that category.
# ---------------------------------------------------------------------------


def test_finalize_attaches_only_the_records_own_category_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    record = _make_finalize_test_record("lab-brief-own-category")  # asset_class="equities"
    storage = {
        "by_asset_class": {
            "stocks": {"brief_version": 1, "macro_themes": ["equities"]},
            "crypto": {"brief_version": 1, "macro_themes": ["digital assets"]},
        }
    }

    result = api_main._finalize_strategy_lab_cycle_record(record, signal_brief_storage=storage)

    assert result.signal_intelligence_brief == storage["by_asset_class"]["stocks"]
    assert "by_asset_class" not in result.signal_intelligence_brief


def test_finalize_attaches_the_pinned_categorys_brief_on_a_short_circuited_off_pin_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ready short-circuit (budget_exhausted/design_stalled/
    design_not_ready) can persist a draft whose authored asset_class never
    converged to the pin -- Rule 11 never validated it. Keying the brief
    lookup off record.strategy.asset_class in that case would attach a
    DIFFERENT category's brief than the one the design agent actually
    received, misattributing the evidence shown on the strategy card.
    loop_telemetry["asset_category"] (set on every _run_design_loop exit
    path, ready or not) must win instead."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    strat = StrategySpec(
        strategy_id="strat-off-pin-draft",
        authored_by="x",
        asset_class="crypto",  # never converged to the "stocks" pin
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result_metrics = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-off-pin-draft",
        strategy_id=strat.strategy_id,
        strategy=strat,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-02-01"),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        status="failed: budget_exhausted",
        result=result_metrics,
        trades=[],
    )
    record = StrategyLabRecord(
        lab_record_id="lab-off-pin-draft",
        strategy=strat,
        backtest=bt,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="budget exhausted",
        created_at="2024-01-01T01:00:00Z",
        loop_telemetry={"asset_category": "stocks"},
    )
    storage = {
        "by_asset_class": {
            "stocks": {"brief_version": 1, "macro_themes": ["equities"]},
            "crypto": {"brief_version": 1, "macro_themes": ["digital assets"]},
        }
    }

    out = api_main._finalize_strategy_lab_cycle_record(record, signal_brief_storage=storage)

    assert out.signal_intelligence_brief == storage["by_asset_class"]["stocks"]


def test_finalize_stores_degraded_skip_marker_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-per-category ``signal_brief_storage`` (the disabled/degraded skip
    marker, which carries no ``by_asset_class`` key) is stored verbatim,
    matching pre-per-category behavior."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    record = _make_finalize_test_record("lab-brief-skip-marker")
    storage = {"skipped": True, "skipped_reason": "signal_expert_disabled"}

    result = api_main._finalize_strategy_lab_cycle_record(record, signal_brief_storage=storage)

    assert result.signal_intelligence_brief == storage


def test_finalize_never_attaches_a_skip_marker_as_brief_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-category entry that is itself a ``{"skipped": True, ...}``
    marker (no prior records, or that category's expert call failed) is not
    brief content -- the strategy card renders whatever is stored here as
    brief fields, so storing the marker verbatim would display
    "skipped"/"skipped_reason"/"error" as if they were macro themes."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    record = _make_finalize_test_record("lab-brief-skip-marker-own-category")  # asset_class="equities"
    storage = {
        "by_asset_class": {"stocks": {"skipped": True, "skipped_reason": "no_prior_records"}}
    }

    result = api_main._finalize_strategy_lab_cycle_record(record, signal_brief_storage=storage)

    assert result.signal_intelligence_brief is None


def test_finalize_leaves_brief_unset_when_records_category_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the record's own category has no entry in the per-category map
    (defensive — every allowed category should always be present), the
    record's brief is left unset rather than populated with another
    category's data."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    record = _make_finalize_test_record("lab-brief-missing-category")  # asset_class="equities"
    storage = {"by_asset_class": {"crypto": {"brief_version": 1}}}

    result = api_main._finalize_strategy_lab_cycle_record(record, signal_brief_storage=storage)

    assert result.signal_intelligence_brief is None


def test_finalize_does_not_overwrite_an_existing_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    record = _make_finalize_test_record("lab-brief-preexisting")
    record.signal_intelligence_brief = {"brief_version": 1, "macro_themes": ["already set"]}
    storage = {"by_asset_class": {"stocks": {"brief_version": 1, "macro_themes": ["new"]}}}

    result = api_main._finalize_strategy_lab_cycle_record(record, signal_brief_storage=storage)

    assert result.signal_intelligence_brief == {"brief_version": 1, "macro_themes": ["already set"]}


def test_finalize_strategy_lab_cycle_record_isolates_raising_on_phase_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising on_phase callback must not abort finalization: the callback's
    exception is caught/logged, persistence still runs, and the record is
    still returned — matching the documented postcondition."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    record = _make_finalize_test_record("lab-finalize-callback-boom")

    def _boom_on_phase(phase: str, data: Dict[str, Any]) -> None:
        raise RuntimeError("callback exploded")

    result = api_main._finalize_strategy_lab_cycle_record(record, on_phase=_boom_on_phase)

    assert result is record
    assert result.paper_trading_status == "skipped"
    assert result.paper_trading_skipped_reason == "not_winning"
    # Persistence must have run despite the callback raising.
    assert api_main._strategy_lab_records["lab-finalize-callback-boom"] is record


def test_finalize_strategy_lab_cycle_record_logs_full_traceback_on_paper_trading_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression test for a non-fatal paper-trading failure's log record:
    it must be logged with ``logger.exception`` (ERROR level + attached
    traceback), not ``logger.warning(..., exc)`` (WARNING level, exception
    text folded into the message, no traceback). The latter made non-fatal
    paper-trading crashes hard to debug from logs alone."""
    import logging

    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_backtests", {})

    def _boom_paper_trading_step(**kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(api_main, "_run_paper_trading_step", _boom_paper_trading_step)

    record = _make_finalize_test_record(
        "lab-finalize-paper-trading-boom",
        is_winning=True,
        is_publishable=True,
        strategy_code="def strategy(): pass",
    )

    events: List[tuple[str, Dict[str, Any]]] = []

    def _record_on_phase(phase: str, data: Dict[str, Any]) -> None:
        events.append((phase, data))

    with caplog.at_level(logging.WARNING, logger=api_main.logger.name):
        result = api_main._finalize_strategy_lab_cycle_record(record, on_phase=_record_on_phase)

    assert result.paper_trading_status == "failed"
    assert result.paper_trading_error == "boom"
    assert ("paper_trading_failed", {"detail": "boom"}) in events

    matching = [
        r for r in caplog.records if "Paper trading step failed (non-fatal)" in r.getMessage()
    ]
    assert len(matching) == 1
    log_record = matching[0]
    # Level must be ERROR (logger.exception), not WARNING (the old logger.warning call).
    assert log_record.levelno == logging.ERROR
    # The message itself must not have the exception text interpolated in —
    # the old call was `logger.warning("...: %s", exc)`.
    assert log_record.getMessage() == "Paper trading step failed (non-fatal)"
    # The traceback must be attached — the old call passed no exc_info.
    assert log_record.exc_info is not None
    assert log_record.exc_info[1] is not None
    assert "RuntimeError" in (log_record.exc_text or "")
    assert "boom" in (log_record.exc_text or "")


def test_normalize_persisted_job_uses_dict_data_field() -> None:
    """A dict ``"data"`` payload is used (and mutated in place) as-is."""
    from investment_team.strategy_lab.run_state import normalize_persisted_job

    job = {"job_id": "job-1", "status": "running", "data": {"completed_cycles": 3}}
    result = normalize_persisted_job(job, fallback_status="completed", run_id="job-1")

    assert result is job["data"]
    assert result["run_id"] == "job-1"
    assert result["status"] == "running"
    assert result["completed_cycles"] == 3


def test_normalize_persisted_job_falls_back_to_job_when_data_absent() -> None:
    """No ``"data"`` key at all -- ``job`` itself is treated as the state dict."""
    from investment_team.strategy_lab.run_state import normalize_persisted_job

    job = {"job_id": "job-1", "status": "running"}
    result = normalize_persisted_job(job, fallback_status="completed", run_id="job-1")

    assert result is job
    assert result["run_id"] == "job-1"


def test_normalize_persisted_job_falls_back_to_job_when_data_is_none() -> None:
    """A ``"data"`` key present but ``None`` must not raise ``TypeError`` --
    regression test for issue #4325."""
    from investment_team.strategy_lab.run_state import normalize_persisted_job

    job = {"job_id": "job-1", "status": "running", "data": None}
    result = normalize_persisted_job(job, fallback_status="completed", run_id="job-1")

    assert result is job
    assert result["run_id"] == "job-1"
    assert result["status"] == "running"


def test_normalize_persisted_job_falls_back_to_job_when_data_is_not_a_dict() -> None:
    """A ``"data"`` key present but holding a non-dict value (e.g. malformed
    job-service JSON) must not raise ``AttributeError``/``TypeError`` either."""
    from investment_team.strategy_lab.run_state import normalize_persisted_job

    job = {"job_id": "job-1", "status": "running", "data": "not-a-dict"}
    result = normalize_persisted_job(job, fallback_status="completed", run_id="job-1")

    assert result is job
    assert result["run_id"] == "job-1"


def test_normalize_persisted_job_derives_run_id_when_not_given() -> None:
    from investment_team.strategy_lab.run_state import normalize_persisted_job

    job = {"job_id": "job-1", "status": "running"}
    result = normalize_persisted_job(job, fallback_status="completed")

    assert result["run_id"] == "job-1"


def test_normalize_persisted_job_defaults_status_from_fallback() -> None:
    """When neither the data dict nor ``job`` itself has a ``status``, the
    caller-supplied ``fallback_status`` is used."""
    from investment_team.strategy_lab.run_state import normalize_persisted_job

    job = {"job_id": "job-1", "data": {}}
    result = normalize_persisted_job(job, fallback_status="completed", run_id="job-1")

    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# _reconcile_run_progress: None "data" tolerance
# ---------------------------------------------------------------------------


def test_reconcile_run_progress_tolerates_none_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persisted record with ``"data": None`` (present but null, distinct
    from the key being absent) must not raise ``TypeError`` -- regression
    test for the same defect class as ``normalize_persisted_job`` but in
    ``_reconcile_run_progress``'s own, separate fallback.

    Preconditions:
        - ``orchestrator_api._active_runs`` / ``_get_lab_run_job_client`` are
          the names the moved body closes over (not ``api.main`` aliases).
    Postconditions:
        - Call returns without ``TypeError``; in-memory ``completed_cycles``
          stays unchanged when the None-data fallback has no progress fields.
    """
    from investment_team.strategy_lab import orchestrator_api

    run_id = "run-none-data"
    shared_runs = {
        run_id: {
            "run_id": run_id,
            "status": "running",
            "total_cycles": 4,
            "completed_cycles": 1,
        }
    }
    monkeypatch.setattr(orchestrator_api, "_active_runs", shared_runs)

    class _Stub:
        def __init__(self) -> None:
            self.calls = 0

        def get_job(self, jid: str):
            self.calls += 1
            return {"job_id": run_id, "status": "running", "data": None}

    stub = _Stub()
    monkeypatch.setattr(orchestrator_api, "_get_lab_run_job_client", lambda: stub)

    orchestrator_api._reconcile_run_progress(run_id)

    # No crash, and the in-memory entry's existing progress is left intact
    # since the fallback ("data" -> the persisted record itself) contains
    # none of _STRATEGY_LAB_PROGRESS_FIELDS.
    assert stub.calls == 1
    assert shared_runs[run_id]["completed_cycles"] == 1


# ---------------------------------------------------------------------------
# _run_state_to_response: missing run_id tolerance
# ---------------------------------------------------------------------------


def test_run_state_to_response_tolerates_missing_run_id() -> None:
    """A state dict without a ``run_id`` key must not raise ``KeyError`` --
    every other field in this function already degrades to a default, and
    ``run_id`` (guaranteed by construction today, but not schema-validated)
    should be defended the same way instead of assuming the invariant can
    never be violated."""
    from investment_team.api.main import _run_state_to_response

    resp = _run_state_to_response({"status": "running"})

    assert resp.run_id == ""
    assert resp.status == "running"


@pytest.mark.parametrize("bad_generation", [None, "", 0, -3, True, 2.5, "x", [], {}])
def test_run_state_to_response_coerces_uninitialized_generation(bad_generation) -> None:
    """An explicit null/empty/non-positive/unparseable generation must degrade
    to DEFAULT_FENCING_GENERATION rather than raising ValidationError --
    status/list routes feed job-service-shaped state through this helper and
    must keep their always-200 contract when a persisted record carries a
    null generation field."""
    from investment_team.api.main import _run_state_to_response
    from investment_team.strategy_lab.run_state import DEFAULT_FENCING_GENERATION

    resp = _run_state_to_response(
        {
            "run_id": "run-null-gen",
            "status": "running",
            "generation": bad_generation,
        }
    )

    assert resp.generation == DEFAULT_FENCING_GENERATION


def test_run_state_to_response_preserves_positive_generation() -> None:
    """A positive integer generation must pass through unchanged."""
    from investment_team.api.main import _run_state_to_response

    resp = _run_state_to_response({"run_id": "run-g", "status": "running", "generation": 4})

    assert resp.generation == 4
