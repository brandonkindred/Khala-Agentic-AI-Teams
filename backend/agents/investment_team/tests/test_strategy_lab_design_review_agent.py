"""Contract tests for :class:`DesignReviewAgent`.

The reviewer:
* Returns a :class:`SpecCritique`.
* Surfaces deterministic readiness findings into the prompt and the
  resulting critique's ``readiness_findings`` audit trail.
* Falls closed (``ready=False`` with a single critical
  ``review_parse_error`` issue) on any LLM / parse fault — the design
  loop never stalls on a reviewer hiccup.
"""

from __future__ import annotations

import json
from typing import Any, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.agents.design_review import (
    DesignReviewAgent,
    SpecCritique,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


class _CapturingAgent:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._payload


class _RaisingAgent:
    def __call__(self, _prompt: str) -> str:
        raise RuntimeError("transport down")


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-review-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )


def _readiness(passed: bool, severity: str, details: str) -> QualityGateResult:
    return QualityGateResult(
        gate_name="spec_readiness",
        passed=passed,
        severity=severity,  # type: ignore[arg-type]
        phase="design",
        details=details,
    )


def _patch_review(
    monkeypatch: pytest.MonkeyPatch, payload: str | None = None, *, raise_: bool = False
) -> _CapturingAgent | _RaisingAgent:
    agent: Any
    if raise_:
        agent = _RaisingAgent()
    else:
        assert payload is not None
        agent = _CapturingAgent(payload)
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design_review.Agent",
        lambda **_kwargs: agent,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design_review.get_strands_model",
        lambda role: object(),
    )
    return agent


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_clean_spec_returns_ready_true(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"ready": True, "rationale": "spec is implementable", "issues": []})
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert critique.rationale == "spec is implementable"
    assert critique.issues == []


def test_dirty_spec_returns_ready_false_with_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "thesis incoherent with entry rule",
            "issues": [
                {
                    "field": "entry_rules",
                    "severity": "critical",
                    "description": "Mean-reversion entry on a trend hypothesis.",
                    "suggested_fix": "Switch to SMA crossover entry.",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert len(critique.issues) == 1
    issue = critique.issues[0]
    assert issue.field == "entry_rules"
    assert issue.severity == "critical"
    assert issue.suggested_fix == "Switch to SMA crossover entry."


def test_readiness_findings_persisted_on_critique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic findings the reviewer was shown are persisted on
    the critique so the audit trail captures the full input set."""
    payload = json.dumps({"ready": True, "rationale": "fine", "issues": []})
    _patch_review(monkeypatch, payload)

    findings = [
        _readiness(False, "warning", "sizing fraction is tight but realisable"),
        _readiness(True, "info", "all indicators known"),
    ]
    critique = DesignReviewAgent().run(_spec(), readiness_results=findings)

    assert critique.readiness_findings == [
        "warning: sizing fraction is tight but realisable",
        "info: all indicators known",
    ]


def test_readiness_block_reaches_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"ready": True, "rationale": "", "issues": []})
    capture = _patch_review(monkeypatch, payload)

    findings = [_readiness(False, "critical", "no entry rule")]
    DesignReviewAgent().run(_spec(), readiness_results=findings)

    assert isinstance(capture, _CapturingAgent)
    prompt = capture.calls[0]
    assert "no entry rule" in prompt
    assert "critical" in prompt


def test_prior_critiques_appear_in_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"ready": True, "rationale": "", "issues": []})
    capture = _patch_review(monkeypatch, payload)

    prior = [SpecCritique(ready=False, rationale="Round-0 concern", round=0)]
    DesignReviewAgent().run(_spec(), readiness_results=[], prior_critiques=prior)

    assert isinstance(capture, _CapturingAgent)
    prompt = capture.calls[0]
    assert "Round 0" in prompt
    assert "Round-0 concern" in prompt


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_parse_failure_falls_closed_to_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_review(monkeypatch, payload="not valid JSON at all")

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert critique.issues  # at least one
    assert any("review_parse_error" in i.description for i in critique.issues)
    assert any(i.severity == "critical" for i in critique.issues)


def test_transport_failure_falls_closed_to_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_review(monkeypatch, raise_=True)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert any("review_parse_error" in i.description for i in critique.issues)


# ---------------------------------------------------------------------------
# Coercion / tolerance of mild schema drift
# ---------------------------------------------------------------------------


def test_unknown_field_value_remapped_to_hypothesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "drift",
            "issues": [
                {
                    "field": "garbage_field",
                    "severity": "warning",
                    "description": "weird issue",
                    "suggested_fix": "",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.issues[0].field == "hypothesis"


def test_unknown_severity_clamped_to_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "drift",
            "issues": [
                {
                    "field": "hypothesis",
                    "severity": "nuclear",
                    "description": "weird severity",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.issues[0].severity == "warning"


def test_not_ready_with_empty_issues_gets_synthetic_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ready=False`` + no issues is incoherent — the agent inserts a
    placeholder so the designer's ``revise()`` has something to act on."""
    payload = json.dumps({"ready": False, "rationale": "vibes", "issues": []})
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert critique.issues
    assert "vibes" in critique.issues[0].description


def test_non_dict_issues_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single bad issue entry should not bin the rest of the critique."""
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "mixed",
            "issues": [
                "this is a string, not a dict",
                {
                    "field": "exit_rules",
                    "severity": "warning",
                    "description": "no take_profit alongside stop_loss",
                },
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    # Exactly the one dict-shaped issue survives.
    assert len(critique.issues) == 1
    assert critique.issues[0].field == "exit_rules"


def test_issue_with_missing_fields_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``{"description": ...}`` becomes a hypothesis/warning issue."""
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "minimal",
            "issues": [{"description": "thesis too thin"}],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.issues[0].field == "hypothesis"
    assert critique.issues[0].severity == "warning"
    assert critique.issues[0].description == "thesis too thin"
