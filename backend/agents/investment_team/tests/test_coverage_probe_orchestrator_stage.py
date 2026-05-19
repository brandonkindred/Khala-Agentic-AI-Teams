"""Orchestrator-wiring tests for the coverage-probe stage (#451).

Tests the orchestrator's ``_maybe_attach_coverage_report`` helper and
the source-level invariants over each ``run_strategy_code`` /
``anomaly_detector.check`` / ``zero_trade_repair_agent.run`` call site.

Pure aggregator unit tests live in ``test_coverage_probe_aggregator.py``;
the ``format_coverage_report`` renderer's unit tests live in
``test_coverage_probe_rendering.py``; full pipeline integration in
``test_coverage_probe_stage.py``.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from investment_team.models import BacktestResult, CoverageCategory, CoverageReport
from investment_team.strategy_lab import orchestrator as orch_mod
from investment_team.strategy_lab.orchestrator import _maybe_attach_coverage_report

from ._coverage_probe_test_helpers import (
    make_config,
    make_diag,
    make_exec_result,
    make_flat_df,
    make_report,
    make_spec,
)

# ─────────────────────────────────────────────────────────────────────
# _maybe_attach_coverage_report — gate behaviour
# ─────────────────────────────────────────────────────────────────────


def _zeroed_metrics() -> BacktestResult:
    return BacktestResult(
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


def test_maybe_attach_coverage_report_no_ops_when_gate_off() -> None:
    metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=5.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=2.0,
        win_rate_pct=55.0,
        profit_factor=1.5,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    _maybe_attach_coverage_report(
        metrics=metrics,
        spec=make_spec("X = 1\n"),
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(closed=10)),
    )
    assert metrics.coverage_report is None


def test_maybe_attach_coverage_report_runs_stage_when_gate_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def _spy_run_coverage_stage(**kwargs: Any) -> CoverageReport:
        captured.append(kwargs)
        return make_report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)

    # ``_maybe_attach_coverage_report`` was hoisted into
    # ``_orchestrator_helpers`` so the helpers module — not the orchestrator
    # module — is the actual lookup site for ``run_coverage_stage``. The
    # orchestrator still re-exports the helper for backwards compatibility,
    # but the monkeypatch must hit the module where the lookup happens.
    from investment_team.strategy_lab import _orchestrator_helpers as helpers_mod

    monkeypatch.setattr(helpers_mod, "run_coverage_stage", _spy_run_coverage_stage)

    metrics = _zeroed_metrics()
    spec = make_spec("Y = 2\n")
    _maybe_attach_coverage_report(
        metrics=metrics,
        spec=spec,
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
    )
    assert metrics.coverage_report is not None
    assert (
        metrics.coverage_report.coverage_category
        is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    )
    assert len(captured) == 1
    assert captured[0]["spec"] is spec


# ─────────────────────────────────────────────────────────────────────
# Source-level invariant — every call site pairs the right spec
# ─────────────────────────────────────────────────────────────────────


def test_orchestrator_call_sites_use_consistent_spec_and_exec_result() -> None:
    """Every ``_maybe_attach_coverage_report`` call site must pair an
    ``exec_result`` with the spec whose ``strategy_code`` matches the
    code that produced it. Lock the AST shape across both the orchestrator
    and the zero-trade repair module so a future refactor that
    re-introduces the spec/exec drift fails loudly here.
    """
    from investment_team.strategy_lab import zero_trade_repair as zt_repair_mod

    expected_spec_for_exec = {
        "exec_result": "spec",
        "align_exec": "proposed_spec",
        "repair_exec": "proposed_spec",
    }

    sites: list[dict[str, Any]] = []
    for module in (orch_mod, zt_repair_mod):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "_maybe_attach_coverage_report":
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            spec_arg = kwargs.get("spec")
            exec_arg = kwargs.get("exec_result")
            lineno = node.lineno
            modname = module.__name__.rsplit(".", 1)[-1]
            assert isinstance(spec_arg, ast.Name), (
                f"{modname}.py:{lineno} — spec kwarg must be a bare Name reference"
            )
            assert isinstance(exec_arg, ast.Name), (
                f"{modname}.py:{lineno} — exec_result kwarg must be a bare Name reference"
            )
            sites.append({"spec": spec_arg.id, "exec_result": exec_arg.id, "lineno": lineno})

    assert len(sites) == 3, f"expected 3 _maybe_attach_coverage_report sites, found {sites}"
    for site in sites:
        expected_spec = expected_spec_for_exec.get(site["exec_result"])
        assert expected_spec is not None, (
            f"orchestrator.py:{site['lineno']} — unknown exec_result variable "
            f"{site['exec_result']!r}; if a new orchestrator call site is "
            f"intentional, update expected_spec_for_exec in this test"
        )
        assert site["spec"] == expected_spec, (
            f"orchestrator.py:{site['lineno']} — _maybe_attach_coverage_report "
            f"site mismatched spec/exec_result: got spec={site['spec']!r} with "
            f"exec_result={site['exec_result']!r}; expected spec={expected_spec!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Source-level invariants — anomaly-detector + repair-agent forwarding
# ─────────────────────────────────────────────────────────────────────


def _collect_attribute_call_sites(
    source: str,
    *,
    owner_attr: str,
    method_attr: str,
    root_names: frozenset = frozenset({"self"}),
) -> list[dict[str, Any]]:
    """Walk ``source`` and return every ``<root>.{owner_attr}.{method_attr}(...)``
    call as ``{lineno, kwargs}``. Used by the source-level invariants
    below; the AST shape keeps tests independent of import paths and
    avoids false positives from unrelated ``.check``/``.run`` methods.

    ``root_names`` controls which leftmost tokens count as the receiver:
    in :mod:`orchestrator` it's ``{"self"}``; in :mod:`zero_trade_repair`
    the gate/agent collaborators live on ``self._orch`` (locally bound to
    ``orch``) so ``{"orch", "self"}`` covers both shapes.
    """
    tree = ast.parse(source)
    sites: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != method_attr:
            continue
        owner = func.value
        if not isinstance(owner, ast.Attribute) or owner.attr != owner_attr:
            continue
        # Pin the leftmost token so an unrelated alias doesn't match.
        if not isinstance(owner.value, ast.Name) or owner.value.id not in root_names:
            continue
        sites.append({"lineno": node.lineno, "kwargs": {kw.arg for kw in node.keywords}})
    return sites


_HELPER_SELFTEST_SOURCE = """\
class Orch:
    def f(self, report):
        self.anomaly_detector.check(metrics, [], coverage_report=report)
        self.anomaly_detector.check(metrics, trades)  # no kwarg
        self.zero_trade_repair_agent.run(spec, code, coverage_report=report)
        other.anomaly_detector.check(metrics, [])     # not self → ignored
        self.something_else.check(metrics, [])        # owner mismatch
        self.anomaly_detector.run(metrics, [])        # method mismatch
"""


def test_collect_attribute_call_sites_helper_isolates_matching_calls() -> None:
    """The two invariants below share ``_collect_attribute_call_sites``; an
    off-by-one in its AST walk would flip both tests together. Pin the
    helper's behaviour against an inline fixture so a regression points
    here first.
    """
    check_sites = _collect_attribute_call_sites(
        _HELPER_SELFTEST_SOURCE, owner_attr="anomaly_detector", method_attr="check"
    )
    assert len(check_sites) == 2
    assert {frozenset(s["kwargs"]) for s in check_sites} == {
        frozenset({"coverage_report"}),
        frozenset(),
    }

    run_sites = _collect_attribute_call_sites(
        _HELPER_SELFTEST_SOURCE, owner_attr="zero_trade_repair_agent", method_attr="run"
    )
    assert len(run_sites) == 1
    assert run_sites[0]["kwargs"] == {"coverage_report"}


def test_every_anomaly_detector_check_call_forwards_coverage_report() -> None:
    """Every ``anomaly_detector.check(...)`` call across the orchestrator
    and the zero-trade repair module must forward ``coverage_report=`` so
    the persisted gate result carries the static probe's verdict.

    Locking this at source level catches the alignment-loop and walk-
    forward-fallback paths that are not directly exercised by the
    existing isolated drivers. Four call sites are expected: main loop +
    alignment loop + walk-forward fallback live on the orchestrator
    (``self.anomaly_detector.check``); the zero-trade-repair recheck
    lives in :mod:`zero_trade_repair` (``orch.anomaly_detector.check``).
    """
    from investment_team.strategy_lab import zero_trade_repair as zt_repair_mod

    orch_sites = _collect_attribute_call_sites(
        inspect.getsource(orch_mod),
        owner_attr="anomaly_detector",
        method_attr="check",
    )
    zt_sites = _collect_attribute_call_sites(
        inspect.getsource(zt_repair_mod),
        owner_attr="anomaly_detector",
        method_attr="check",
        root_names=frozenset({"orch"}),
    )
    sites = orch_sites + zt_sites
    assert len(sites) == 4, (
        "expected exactly 4 anomaly_detector.check sites (main loop, alignment "
        "loop, walk-forward-fallback, zero-trade-repair recheck); if a new "
        f"call site is intentional, bump this assertion. Found: {sites}"
    )
    missing = [s for s in sites if "coverage_report" not in s["kwargs"]]
    assert not missing, (
        "Every anomaly_detector.check call must forward coverage_report=; "
        f"missing on lines {[s['lineno'] for s in missing]}"
    )


def test_repair_agent_run_call_forwards_coverage_report() -> None:
    """The ``zero_trade_repair_agent.run(...)`` call in the repair module
    must forward ``coverage_report=`` so the agent's prompt sees the
    static probe's verdict alongside the executor diagnostics.

    Functional coverage of the forwarding lives in
    ``test_strategy_lab_zero_trade_repair.py``; this AST check locks the
    contract at the source level too so a refactor that drops the kwarg
    fails loudly. The call lives in :mod:`zero_trade_repair`
    (``orch.zero_trade_repair_agent.run``).
    """
    from investment_team.strategy_lab import zero_trade_repair as zt_repair_mod

    sites = _collect_attribute_call_sites(
        inspect.getsource(zt_repair_mod),
        owner_attr="zero_trade_repair_agent",
        method_attr="run",
        root_names=frozenset({"orch"}),
    )
    assert len(sites) == 1, (
        "expected exactly 1 zero_trade_repair_agent.run site; if a new call "
        f"site is intentional, bump this assertion. Found: {sites}"
    )
    missing = [s for s in sites if "coverage_report" not in s["kwargs"]]
    assert not missing, (
        "zero_trade_repair_agent.run must forward coverage_report=; "
        f"missing on lines {[s['lineno'] for s in missing]}"
    )
