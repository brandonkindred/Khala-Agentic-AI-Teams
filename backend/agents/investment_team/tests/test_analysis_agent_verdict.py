"""Verdict-selection contract for ``AnalysisAgent.run``.

The WINNING/LOSING label is deterministic: a strategy wins when its annualized
return meets or beats the 8% S&P-500 benchmark (``>= WINNING_THRESHOLD``).
``AnalysisAgent.run`` applies that rule to pick between ``analysis_win.md`` and
``analysis_lose.md`` and to set ``outcome_label="WINNING"|"LOSING"`` when the
caller does not pass an explicit ``is_winning``. When the caller (the
orchestrator) does pass one — it always passes the deterministic value — the
agent must honour it verbatim rather than re-deriving from the metric. These
tests pin both halves of that contract; the explicit-override cases also guard
against a direct caller's value being silently ignored.

Robustness notes:
- Template selection is verified by recording which key the agent looks up in
  ``analysis._DRAFT_TEMPLATES`` (the templates are loaded once at import) — this
  survives wording edits to ``analysis_win.md`` / ``analysis_lose.md`` (the
  goldens in ``test_analysis_alignment_prompts.py`` pin the actual content).
- ``Outcome label: <LABEL>`` is asserted against the rendered prompt; that
  placeholder lives in ``analysis.py`` itself (the ``_SELF_REVIEW_CHECKLIST``
  constant spliced into the draft prompt), not in a drifting template.
"""

from __future__ import annotations

import json
from typing import Any, List

import pytest

from investment_team.models import BacktestResult, StrategySpec
from investment_team.strategy_lab.agents import _agent_runner as agent_runner_module
from investment_team.strategy_lab.agents import analysis as analysis_module
from investment_team.strategy_lab.agents.alignment import (
    AlignmentIssue,
    TradeAlignmentReport,
)
from investment_team.strategy_lab.agents.analysis import AnalysisAgent
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    StopLossRule,
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
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )


def _high_return_metrics() -> BacktestResult:
    # 15% annualized — at or above the 8% benchmark, so the deterministic
    # rule classifies this WINNING when no explicit verdict is passed.
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

    def render_response(self) -> str:
        return json.dumps({"draft_narrative": "draft body"})


def _install_recorder(monkeypatch) -> _Recorder:
    """Patch ``strands.Agent``, the strands model factory, and the draft-template
    map so the test can capture prompt text + template selection without hitting
    the LLM. Returns the live recorder.

    Draft templates are loaded once at import into ``analysis._DRAFT_TEMPLATES``;
    selection is therefore observed by recording which key the agent looks up
    (still robust to wording edits in either template, like the prior
    ``Path.read_text`` spy)."""

    rec = _Recorder()

    class _StubAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            rec.prompts.append(prompt)
            return rec.render_response()

    class _RecordingTemplates(dict):
        def __getitem__(self, key):
            rec.read_files.append(key)
            return super().__getitem__(key)

    monkeypatch.setattr(agent_runner_module, "Agent", _StubAgent)
    monkeypatch.setattr(agent_runner_module, "get_strands_model", lambda _name: None)
    monkeypatch.setattr(
        analysis_module,
        "_DRAFT_TEMPLATES",
        _RecordingTemplates(analysis_module._DRAFT_TEMPLATES),
    )
    return rec


def test_agent_key_is_strategy_analysis_not_ideation(monkeypatch):
    """Regression guard: the single draft/self-review call must identify
    itself as ``strategy_analysis`` (not the mislabeled ``strategy_ideation``
    copied from an unrelated agent) so per-agent telemetry/timeout/model
    routing is not mis-attributed."""

    class _StubAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def __call__(self, _prompt: str) -> str:
            return json.dumps({"draft_narrative": "draft"})

    model_keys: List[str] = []
    monkeypatch.setattr(agent_runner_module, "Agent", _StubAgent)
    monkeypatch.setattr(
        agent_runner_module, "get_strands_model", lambda name: model_keys.append(name) or None
    )

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
    )

    assert model_keys == ["strategy_analysis"]


def test_analysis_agent_honours_explicit_is_winning_false_on_high_return(monkeypatch):
    """Even with metrics.annualized_return_pct=15% (the deterministic rule
    would say WINNING), an explicit ``is_winning=False`` from the caller must
    select the LOSING template + ``outcome_label="LOSING"``. Guards the agent
    contract: a passed verdict is never silently overridden by the metric."""

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
    # The outcome_label placeholder lives in analysis.py's _SELF_REVIEW_CHECKLIST
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
    """When no ``is_winning`` is passed the agent applies the deterministic
    rule: 15% annualized is at or above the 8% benchmark → WINNING."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),  # 15% annualized ≥ 8% benchmark → winning
        trades=[],
        rationale="rationale",
    )

    assert "analysis_win.md" in rec.read_files
    assert "analysis_lose.md" not in rec.read_files
    prompts = "\n\n".join(rec.prompts)
    assert "Outcome label: WINNING" in prompts


@pytest.mark.parametrize(
    "annualized,expected_win",
    [
        pytest.param(8.0, True, id="exactly_8pct_is_winning"),
        pytest.param(7.99, False, id="just_below_8pct_is_losing"),
        pytest.param(44.6, True, id="high_return_is_winning"),
    ],
)
def test_analysis_agent_metric_fallback_is_deterministic_at_boundary(
    monkeypatch, annualized, expected_win
):
    """The unset-``is_winning`` fallback is the deterministic >= 8% rule:
    exactly 8.00% wins (it meets the S&P-500 benchmark), 7.99% loses. Pins the
    boundary so the rule cannot silently drift back to a strict ``> 8.0``."""

    rec = _install_recorder(monkeypatch)

    metrics = _high_return_metrics().model_copy(update={"annualized_return_pct": annualized})
    AnalysisAgent().run(_spec(), metrics, trades=[], rationale="rationale")

    expected_file = "analysis_win.md" if expected_win else "analysis_lose.md"
    forbidden_file = "analysis_lose.md" if expected_win else "analysis_win.md"
    assert expected_file in rec.read_files
    assert forbidden_file not in rec.read_files
    label = "WINNING" if expected_win else "LOSING"
    assert f"Outcome label: {label}" in "\n\n".join(rec.prompts)


def test_analysis_agent_threads_robustness_caveats_from_metrics(monkeypatch):
    """A run carrying a recorded robustness concern (acceptance_reason) must
    inject the ``## Robustness caveats`` block into the draft prompt so a
    winner's narrative can cite the risk without reclassifying the verdict —
    the reported high-return-but-fragile case."""

    rec = _install_recorder(monkeypatch)

    metrics = _high_return_metrics().model_copy(
        update={
            "annualized_return_pct": 44.6,
            "acceptance_reason": "OOS DSR 0.30 below threshold 1.000",
            "oos_sharpe": 0.40,
        }
    )
    AnalysisAgent().run(_spec(), metrics, trades=[], rationale="rationale", is_winning=True)

    assert "analysis_win.md" in rec.read_files
    for prompt in rec.prompts:
        assert "## Robustness caveats" in prompt
        assert "OOS DSR 0.30 below threshold" in prompt


def test_analysis_agent_omits_caveats_on_clean_metrics(monkeypatch):
    """A clean run (no recorded robustness concern) injects no caveat block,
    keeping the prompt byte-identical to the pre-caveats behaviour."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(), _high_return_metrics(), trades=[], rationale="rationale", is_winning=True
    )

    for prompt in rec.prompts:
        assert "## Robustness caveats" not in prompt


def test_analysis_agent_honours_explicit_robustness_caveats_override(monkeypatch):
    """An explicit ``robustness_caveats`` argument overrides the metric-derived
    block, so callers (and tests) can inject a pre-rendered section."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
        robustness_caveats="## Robustness caveats\n- injected marker\n",
    )

    for prompt in rec.prompts:
        assert "injected marker" in prompt


def test_misaligned_alignment_report_threads_disclaimer_into_draft(
    monkeypatch,
):
    """Issue #532: when the orchestrator passes an ``alignment_report`` with
    ``aligned=False``, the single draft prompt must surface the disclaimer +
    each concrete issue description. Without this the LLM keeps writing
    confident causal narratives even when the trades didn't implement the
    spec."""

    rec = _install_recorder(monkeypatch)

    report = TradeAlignmentReport(
        aligned=False,
        rationale="Stop-loss skipped; trade entered outside the universe.",
        issues=[
            AlignmentIssue(
                rule_type="exit_rules",
                severity="critical",
                description="stop-loss did not fire on trade #4 despite -8% drawdown",
                affected_trades=[4],
            ),
            AlignmentIssue(
                rule_type="universe",
                severity="warning",
                description="trade #7 used symbol outside the spec universe",
                affected_trades=[7],
            ),
        ],
    )

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=report,
    )

    # Exactly one LLM call is expected now that draft + self-review are merged.
    assert len(rec.prompts) == 1, (
        f"Expected exactly one LLM call (self-reviewing draft); got {len(rec.prompts)}."
    )
    prompt = rec.prompts[0]
    assert "TRADES DID NOT IMPLEMENT THE SPEC" in prompt, (
        "draft prompt missing misalignment header (issue #532)."
    )
    assert (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary." in prompt
    ), "draft prompt missing verbatim disclaimer (issue #532)."
    for issue in report.issues:
        assert issue.description in prompt, (
            f"draft prompt dropped alignment issue {issue.description!r} (issue #532)."
        )
    assert "DO NOT make causal claims about strategy design" in prompt, (
        "draft prompt missing 'no causal claims' instruction (issue #532)."
    )


def test_aligned_report_does_not_inject_disclaimer(monkeypatch):
    """Issue #532: ``aligned=True`` must NOT inject the misalignment
    disclaimer into either prompt (winning runs aren't disclaimed)."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
        alignment_report=TradeAlignmentReport(aligned=True),
    )

    prompts = "\n\n".join(rec.prompts)
    assert "TRADES DID NOT IMPLEMENT THE SPEC" not in prompts
    assert "did not faithfully implement the specification" not in prompts
    # The one-line clean affirmation IS expected on aligned runs.
    assert "alignment audit clean" in prompts


def test_no_alignment_report_omits_section_entirely(monkeypatch):
    """Issue #532: legacy callers (and the orchestrator fallback path that
    runs without any alignment report) must produce prompts that contain
    neither the disclaimer nor the clean affirmation — just the legacy
    body, byte-for-byte minus the empty placeholder."""

    rec = _install_recorder(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
        # alignment_report omitted (defaults to None)
    )

    prompts = "\n\n".join(rec.prompts)
    assert "## Alignment status" not in prompts
    assert "TRADES DID NOT IMPLEMENT THE SPEC" not in prompts
    assert "alignment audit clean" not in prompts


# ---------------------------------------------------------------------------
# Issue #532 (Codex follow-up): the deterministic fallback narrative must
# carry the misalignment disclaimer + issues forward when the LLM draft path
# fails. Otherwise a fail-closed audit error or transient draft outage would
# publish a confident auto-summary on a run that didn't implement the spec.
# ---------------------------------------------------------------------------


def _install_failing_draft_recorder(monkeypatch, *, mode: str) -> _Recorder:
    """Patch ``strands.Agent`` so the sole draft/self-review call fails per
    ``mode``. Mirrors the production failure modes Codex flagged:

    * ``raise``  — the LLM call raises
    * ``junk``   — the LLM returns non-JSON text (``extract_json_object`` raises)
    * ``empty``  — the LLM returns valid JSON with an empty ``draft_narrative``
    """

    rec = _Recorder()

    class _FailingDraftAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            rec.prompts.append(prompt)
            if mode == "raise":
                raise RuntimeError("simulated draft transport failure")
            if mode == "junk":
                return "this is not json"
            if mode == "empty":
                return json.dumps({"draft_narrative": ""})
            raise AssertionError(f"unknown mode {mode!r}")

    monkeypatch.setattr(agent_runner_module, "Agent", _FailingDraftAgent)
    monkeypatch.setattr(agent_runner_module, "get_strands_model", lambda _name: None)
    return rec


_MISALIGNED_REPORT = TradeAlignmentReport(
    aligned=False,
    rationale="Stop-loss skipped; trade entered outside the universe.",
    issues=[
        AlignmentIssue(
            rule_type="exit_rules",
            severity="critical",
            description="stop-loss did not fire on trade #4 despite -8% drawdown",
            affected_trades=[4],
        ),
        AlignmentIssue(
            rule_type="universe",
            severity="warning",
            description="trade #7 used symbol outside the spec universe",
            affected_trades=[7],
        ),
    ],
)


def _assert_misaligned_fallback(narrative: str) -> None:
    assert (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary." in narrative
    ), "Fallback narrative dropped the misalignment disclaimer (issue #532)."
    for issue in _MISALIGNED_REPORT.issues:
        assert issue.description in narrative, (
            f"Fallback narrative dropped alignment issue {issue.description!r} (issue #532)."
        )
    assert "Detailed narrative generation failed" in narrative, (
        "Fallback narrative dropped the deterministic auto-summary tail."
    )


@pytest.mark.parametrize(
    "mode", ["raise", "junk", "empty"], ids=["draft_raises", "draft_junk", "draft_empty"]
)
def test_misaligned_disclaimer_survives_draft_failure(monkeypatch, mode):
    """Codex review on #532: when ``aligned=False`` and the draft LLM call
    raises, returns unparseable JSON, or yields an empty narrative, the
    deterministic fallback must still surface the disclaimer + each audit
    issue so misaligned runs cannot publish a clean auto-summary."""

    _install_failing_draft_recorder(monkeypatch, mode=mode)

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=_MISALIGNED_REPORT,
    )

    _assert_misaligned_fallback(narrative)


def test_aligned_fallback_unchanged_on_draft_failure(monkeypatch):
    """Aligned (or absent) reports must not inject any disclaimer into the
    fallback narrative — keeps clean runs byte-identical to pre-#532
    behaviour when the LLM happens to fail."""

    _install_failing_draft_recorder(monkeypatch, mode="raise")

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
        alignment_report=TradeAlignmentReport(aligned=True),
    )

    assert "did not faithfully implement the specification" not in narrative
    assert "Alignment issues:" not in narrative
    assert "Detailed narrative generation failed" in narrative


def test_no_report_fallback_unchanged_on_draft_failure(monkeypatch):
    """Legacy callers (no ``alignment_report``) get the original fallback
    text byte-for-byte — back-compat guard."""

    _install_failing_draft_recorder(monkeypatch, mode="empty")

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
    )

    assert "did not faithfully implement the specification" not in narrative
    assert "Alignment issues:" not in narrative
    assert "Detailed narrative generation failed" in narrative


# ---------------------------------------------------------------------------
# Issue #532 Codex follow-up (PR #584): the LLM cannot be trusted to follow
# the "open with the disclaimer verbatim" instruction embedded in its prompt.
# ``_ensure_misalignment_disclaimer`` is the deterministic safety rail that
# prepends the prefix on misaligned runs whenever the published narrative is
# missing the disclaimer string.
# ---------------------------------------------------------------------------


def _install_compliant_review_recorder(monkeypatch, *, draft_body: str) -> _Recorder:
    """Stubs ``strands.Agent`` so the sole draft/self-review call returns
    ``draft_body`` (the test controls whether it contains the disclaimer).
    Captures the prompt in the recorder."""

    rec = _Recorder()

    class _StubAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            rec.prompts.append(prompt)
            return json.dumps({"draft_narrative": draft_body})

    monkeypatch.setattr(agent_runner_module, "Agent", _StubAgent)
    monkeypatch.setattr(agent_runner_module, "get_strands_model", lambda _name: None)
    return rec


def test_misaligned_disclaimer_prepended_when_draft_drops_it(monkeypatch):
    """Codex on PR #584: the LLM can ignore the disclaimer instruction. The
    agent must deterministically prepend the prefix when the narrative is
    missing the disclaimer string so a non-compliant LLM cannot publish a
    clean narrative on a misaligned run."""

    _install_compliant_review_recorder(
        monkeypatch,
        draft_body="The strategy succeeded because of strong trend persistence.",
    )

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=_MISALIGNED_REPORT,
    )

    assert (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary." in narrative
    )
    for issue in _MISALIGNED_REPORT.issues:
        assert issue.description in narrative
    assert "strong trend persistence" in narrative


def test_misaligned_no_modification_when_disclaimer_and_all_issues_present(monkeypatch):
    """Fully-compliant LLM output (opens with the disclaimer AND includes
    every concrete ``AlignmentIssue.description`` somewhere in the body)
    must pass through the rail byte-identically — no duplicated
    disclaimer, no appended issues block."""

    disclaimer = (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary."
    )
    # Fully compliant — opens with the disclaimer and mentions every issue.
    compliant_body = (
        f"{disclaimer} Specifically: "
        "stop-loss did not fire on trade #4 despite -8% drawdown, "
        "and trade #7 used symbol outside the spec universe. "
        "Rerun once aligned."
    )
    # Sanity-check the fixture so a refactor of _MISALIGNED_REPORT.issues
    # surfaces in this test rather than as silent rail behaviour drift.
    for issue in _MISALIGNED_REPORT.issues:
        assert issue.description in compliant_body, (
            f"Test fixture out of sync: {issue.description!r} missing from compliant_body."
        )

    _install_compliant_review_recorder(monkeypatch, draft_body=compliant_body)

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=_MISALIGNED_REPORT,
    )

    assert narrative.count(disclaimer) == 1
    # Neither the prepend prefix nor the deterministic-append block must fire.
    assert "Alignment issues:" not in narrative
    assert "deterministically appended" not in narrative


def test_misaligned_issues_appended_when_llm_opens_but_drops_issues(monkeypatch):
    """Codex on PR #584 (commit 3518419): the LLM may open with the
    disclaimer perfectly but then drop every concrete alignment issue
    from the body. The rail must deterministically append the missing
    issues so operators always see the audit facts — otherwise a
    disclaimer-opening narrative could still bury the substance."""

    disclaimer = (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary."
    )
    # LLM opens with the disclaimer but otherwise paraphrases vaguely
    # without naming any of the concrete issues.
    partial_body = (
        f"{disclaimer} The trades diverged from the design in subtle ways. Rerun once aligned."
    )

    _install_compliant_review_recorder(monkeypatch, draft_body=partial_body)

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=_MISALIGNED_REPORT,
    )

    # Disclaimer still opens (rail didn't prepend a second one).
    assert narrative.lstrip().startswith(disclaimer)
    assert narrative.count(disclaimer) == 1
    # Each missing issue must have been deterministically appended.
    assert "Alignment issues (deterministically appended):" in narrative
    for issue in _MISALIGNED_REPORT.issues:
        assert issue.description in narrative


def test_misaligned_only_missing_issues_appended(monkeypatch):
    """If the LLM mentions *some* issues verbatim but drops others, only
    the dropped ones must be appended — the rail must not duplicate
    issues the LLM already surfaced."""

    disclaimer = (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary."
    )
    # LLM mentions issue #1 verbatim but drops issue #2.
    mentioned_issue = _MISALIGNED_REPORT.issues[0]
    dropped_issue = _MISALIGNED_REPORT.issues[1]
    partial_body = (
        f"{disclaimer} The audit noted: {mentioned_issue.description}. Rerun once aligned."
    )

    _install_compliant_review_recorder(monkeypatch, draft_body=partial_body)

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=_MISALIGNED_REPORT,
    )

    # Mentioned issue must appear exactly once (LLM body); dropped issue
    # must appear once (deterministic append).
    assert narrative.count(mentioned_issue.description) == 1
    assert narrative.count(dropped_issue.description) == 1
    # The deterministic-append block must list ONLY the dropped issue.
    append_idx = narrative.index("Alignment issues (deterministically appended):")
    appended_block = narrative[append_idx:]
    assert dropped_issue.description in appended_block
    assert mentioned_issue.description not in appended_block


def test_misaligned_disclaimer_enforced_when_buried_mid_narrative(monkeypatch):
    """Codex on PR #584 (commit 2c8fcf3): a containment-only check would
    accept narratives that mention the disclaimer *later* in the body but
    open with a confident/causal claim. The safety rail must require the
    disclaimer to OPEN the narrative — anything else gets the full
    prefix prepended."""

    disclaimer = (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary."
    )
    # LLM puts a causal claim first, then mentions the disclaimer later —
    # exactly the bypass Codex flagged.
    buried_body = (
        f"The strategy succeeded because of strong trend persistence. (Aside: {disclaimer})"
    )

    _install_compliant_review_recorder(monkeypatch, draft_body=buried_body)

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
        alignment_report=_MISALIGNED_REPORT,
    )

    # The safety rail must have prepended the full prefix — narrative
    # opens with the disclaimer, not the causal claim.
    assert narrative.lstrip().startswith(disclaimer), (
        "Misaligned narrative must OPEN with the disclaimer; a containment-"
        "only check would let a causal claim slip in first (Codex PR #584)."
    )
    # And the alignment issues must precede the LLM's causal body.
    causal_idx = narrative.index("strong trend persistence")
    for issue in _MISALIGNED_REPORT.issues:
        issue_idx = narrative.index(issue.description)
        assert issue_idx < causal_idx, (
            f"Alignment issue {issue.description!r} must appear before "
            "the LLM's causal claim, not after."
        )


def test_aligned_runs_skip_disclaimer_enforcement(monkeypatch):
    """The safety rail must no-op on aligned (and no-report) runs — clean
    runs stay byte-identical to pre-#532 behaviour even if the LLM happens
    to mention an aligned strategy in non-disclaimer terms."""

    _install_compliant_review_recorder(
        monkeypatch,
        draft_body="The strategy worked because of clear trend signals.",
    )

    narrative = AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=True,
        alignment_report=TradeAlignmentReport(aligned=True),
    )

    assert "did not faithfully implement the specification" not in narrative
    assert "Alignment issues:" not in narrative
    assert "clear trend signals" in narrative
