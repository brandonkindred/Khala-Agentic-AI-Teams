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
* ``_run_backtest_background`` happy + HTTPException + generic-exception
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
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List

import pytest


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


class _InMemoryDict:
    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __contains__(self, k):
        return k in self._d

    def __delitem__(self, k):
        self._d.pop(k, None)

    def pop(self, k, *args):
        if args:
            return self._d.pop(k, args[0])
        return self._d.pop(k)

    def values(self):
        return list(self._d.values())


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


def test_build_strategy_from_ideation_rejects_non_mapping() -> None:
    from investment_team.api.main import _build_strategy_from_ideation

    with pytest.raises(TypeError):
        _build_strategy_from_ideation(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _build_strategy_from_ideation(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _PersistentDict (in-process FakeJobClient roundtrip)
# ---------------------------------------------------------------------------


class _FakeJobClient:
    """Minimal in-memory ``JobServiceClient`` for _PersistentDict tests.

    Thread-safe: the purge/delete helpers under test now issue ``delete_job``
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
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)
    monkeypatch.setattr(
        api_main,
        "_bt_update_job",
        lambda jid, **kw: state.update(kw),
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

    api_main._run_backtest_background("job-1", strategy, config, "tester", [])
    # Final state update is to COMPLETED.
    assert state.get("status") == "completed"
    assert state.get("backtest_id", "").startswith("bt-")


def test_run_backtest_background_handles_http_exception(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from fastapi import HTTPException

    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)
    monkeypatch.setattr(api_main, "_bt_update_job", lambda jid, **kw: state.update(kw))

    def _raises_http(strategy, config):
        raise HTTPException(status_code=422, detail="bad strategy")

    monkeypatch.setattr(api_main, "_run_real_data_backtest", _raises_http)

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
    api_main._run_backtest_background("job-2", strategy, config, "tester", None)
    assert state.get("status") == "failed"
    assert state.get("error") == "bad strategy"


def test_run_backtest_background_handles_generic_exception(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: False)
    monkeypatch.setattr(api_main, "_bt_update_job", lambda jid, **kw: state.update(kw))

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
    api_main._run_backtest_background("job-3", strategy, config, "tester", None)
    assert state.get("status") == "failed"
    assert "network down" in (state.get("error") or "")


def test_run_backtest_background_early_cancellation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import BacktestConfig, StrategySpec

    state: Dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_bt_is_job_cancelled", lambda jid: True)
    monkeypatch.setattr(api_main, "_bt_update_job", lambda jid, **kw: state.update(kw))

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
    api_main._run_backtest_background("job-4", strategy, config, "tester", None)
    # No update calls — early return.
    assert state == {}


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
    fake.create_job("pt-5")  # no job_id when listed? — set explicitly via key
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
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_PURGE_TIMEOUT_S", 0.2)

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
        counts = api_main._purge_strategy_lab_job_storage()
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
        except BaseException as exc:  # pragma: no cover - surfaced via assertion below
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


def test_persist_run_state_propagates_job_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine job-service failure must propagate, not be silently logged
    and swallowed -- callers (run/resume/restart dispatch, and the Temporal
    persist activity's retry policy) need to detect a durable-write failure
    instead of continuing as if it succeeded (issue #4150)."""
    from investment_team.api import main as api_main

    class _Broken:
        def create_job(self, *a, **k):
            raise RuntimeError("backend down")

        def update_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Broken())
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

    client = _FakeJobClient()
    client.create_job("run-cancelled", status="cancelled", completed_cycles=2)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: client)

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


# ---------------------------------------------------------------------------
# run_paper_trading validation branches
# ---------------------------------------------------------------------------


def _winning_record(strategy_code: str | None = "def x(): pass"):
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


def test_run_paper_trading_rejects_losing_strategy(api_client, monkeypatch) -> None:
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


def test_run_paper_trading_rejects_non_publishable_strategy(api_client, monkeypatch) -> None:
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


def test_run_paper_trading_rejects_when_no_strategy_code(api_client, monkeypatch) -> None:
    from investment_team.api import main as api_main

    record = _winning_record(strategy_code=None)
    api_main._strategy_lab_records["lab-w"] = record

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 400
    assert "no generated strategy code" in resp.json()["detail"]


def test_run_paper_trading_kicks_off_background_worker(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy (non-live) path must start a daemon thread and return running."""
    from investment_team.api import main as api_main

    record = _winning_record()
    api_main._strategy_lab_records["lab-w"] = record

    # Replace the background worker so the test doesn't spin up real work.
    started: List[bool] = []
    monkeypatch.setattr(
        api_main, "_run_paper_trading_background", lambda *a, **k: started.append(True)
    )
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
    import time

    for _ in range(20):
        if started:
            break
        time.sleep(0.05)
    assert started == [True]


# ---------------------------------------------------------------------------
# complete_advisor_session — happy path
# ---------------------------------------------------------------------------


def test_complete_advisor_session_builds_ips(api_client) -> None:
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
    from investment_team.api import main as api_main

    calls: List[str] = []
    monkeypatch.setattr(
        api_main, "_bt_mark_all_running_jobs_failed", lambda reason: calls.append(reason)
    )
    monkeypatch.setattr(
        "investment_team.api.job_event_bus.shutdown", lambda: None, raising=False
    )

    api_main._run_investment_service_shutdown()

    assert calls == ["server shutdown"]


def test_shutdown_hook_swallows_job_store_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising failure-sweep must NOT abort shutdown teardown."""
    from investment_team.api import main as api_main

    def _boom(reason: str) -> None:
        raise RuntimeError("job service unreachable")

    monkeypatch.setattr(api_main, "_bt_mark_all_running_jobs_failed", _boom)
    monkeypatch.setattr(
        "investment_team.api.job_event_bus.shutdown", lambda: None, raising=False
    )

    api_main._run_investment_service_shutdown()  # must not raise


# ---------------------------------------------------------------------------
# _finalize_strategy_lab_cycle_record: on_phase callback isolation
# ---------------------------------------------------------------------------


def _make_finalize_test_record(lab_record_id: str) -> Any:
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
        # is_winning=False takes the earliest skip branch, so the finalize
        # call only needs to exercise the on_phase callback + persistence —
        # no paper-trading infra required.
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )


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
