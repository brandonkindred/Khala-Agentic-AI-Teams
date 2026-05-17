"""Regression guard for issue #529 follow-up review (PR #573).

``AnalysisAgent.run`` used to derive its own verdict from
``metrics.annualized_return_pct > 8.0`` to choose between ``analysis_win.md``
and ``analysis_lose.md`` and set ``outcome_label="WINNING"|"LOSING"``. After
#529, the orchestrator can mark a high-return run as ``is_winning=False`` —
the alignment loop, the walk-forward acceptance gate, or the removal of the
legacy ``walk_forward_enabled=False`` publication path can all veto. The
caller now threads the resolved verdict in, and this test pins that the agent
honours it instead of looking at the metric.

Robustness notes:
- Template selection is verified by spying on ``Path.read_text`` to record
  which prompt file was opened — this survives wording edits to
  ``analysis_win.md`` / ``analysis_lose.md`` (the goldens in
  ``test_analysis_alignment_prompts.py`` pin the actual content).
- ``Outcome label: <LABEL>`` is asserted against the rendered self-review
  prompt; that placeholder lives in ``analysis.py`` itself (the
  ``_SELF_REVIEW_PROMPT`` constant), not in a drifting template.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from investment_team.models import BacktestResult, StrategySpec
from investment_team.strategy_lab.agents import analysis as analysis_module
from investment_team.strategy_lab.agents.analysis import AnalysisAgent
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    TimeStopRule,
)


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-test",
        authored_by="test-suite",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            )
        ],
        exit_rules=[TimeStopRule(n_bars=5)],
        risk_limits={},
        speculative=False,
    )


def _high_return_metrics() -> BacktestResult:
    # Above the legacy WINNING_THRESHOLD (8.0) — under the old code path
    # this would force ``is_winning=True`` inside AnalysisAgent.
    return BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=15.0,
        volatility_pct=8.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


class _Recorder:
    """Per-test recorder for prompts sent to the stubbed ``strands.Agent``
    and prompt-file basenames read by the analysis module."""

    def __init__(self) -> None:
        self.prompts: List[str] = []
        self.read_files: List[str] = []
        self._call_count = 0

    def render_response(self) -> str:
        self._call_count += 1
        if self._call_count == 1:
            return json.dumps({"draft_narrative": "draft body"})
        return json.dumps(
            {
                "revised_narrative": "final revised narrative",
                "verification_notes": "checked",
            }
        )


def _install_recorder(monkeypatch) -> _Recorder:
    """Patch ``strands.Agent``, the strands model factory, and
    ``Path.read_text`` so the test can capture prompt text + template
    selection without hitting the LLM. Returns the live recorder."""

    rec = _Recorder()

    class _StubAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            rec.prompts.append(prompt)
            return rec.render_response()

    monkeypatch.setattr(analysis_module, "Agent", _StubAgent)
    monkeypatch.setattr(analysis_module, "get_strands_model", lambda _name: None)

    original_read_text = Path.read_text

    def _spy_read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        rec.read_files.append(self.name)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy_read_text)
    return rec


def test_analysis_agent_honours_explicit_is_winning_false_on_high_return(monkeypatch):
    """Even with metrics.annualized_return_pct=15% (legacy ``is_winning=True``),
    an explicit ``is_winning=False`` from the orchestrator must select the
    LOSING template + ``outcome_label="LOSING"`` so the narrative cannot tell
    users the strategy won."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
    )

    # Template selection: the LOSING file must have been read; the WINNING
    # file must NOT. This is robust to wording edits in either template.
    assert "analysis_lose.md" in rec.read_files, (
        "AnalysisAgent must read analysis_lose.md when is_winning=False is "
        "forced by the orchestrator (issue #529 follow-up)."
    )
    assert "analysis_win.md" not in rec.read_files, (
        "AnalysisAgent must NOT read analysis_win.md when is_winning=False is "
        "forced by the orchestrator (issue #529 follow-up)."
    )
    # The outcome_label placeholder lives in analysis.py's _SELF_REVIEW_PROMPT
    # constant, so this stays stable even if the template files are reworded.
    prompts = "\n\n".join(rec.prompts)
    assert "Outcome label: LOSING" in prompts
    assert "Outcome label: WINNING" not in prompts


def test_analysis_agent_honours_explicit_is_winning_true_on_low_return(monkeypatch):
    """Symmetric guard: when the orchestrator says is_winning=True the
    narrative must use the WINNING template even if metrics alone would
    have rendered LOSING."""

    rec = _install_recorder(monkeypatch)

    low_return = _high_return_metrics().model_copy(
        update={"annualized_return_pct": 3.0, "total_return_pct": 3.5}
    )

    AnalysisAgent().run(
        _spec(),
        low_return,
        trades=[],
        rationale="rationale",
        is_winning=True,
    )

    assert "analysis_win.md" in rec.read_files
    assert "analysis_lose.md" not in rec.read_files
    prompts = "\n\n".join(rec.prompts)
    assert "Outcome label: WINNING" in prompts
    assert "Outcome label: LOSING" not in prompts


def test_analysis_agent_falls_back_to_metric_heuristic_when_unset(monkeypatch):
    """Back-compat: when no ``is_winning`` is passed the legacy
    metric-based derivation is preserved (no behaviour change for callers
    that haven't migrated)."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),  # 15% annualized → legacy heuristic says winning
        trades=[],
        rationale="rationale",
    )

    assert "analysis_win.md" in rec.read_files
    assert "analysis_lose.md" not in rec.read_files
    prompts = "\n\n".join(rec.prompts)
    assert "Outcome label: WINNING" in prompts
