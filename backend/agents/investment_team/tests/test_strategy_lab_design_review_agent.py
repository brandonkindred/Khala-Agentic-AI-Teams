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
    CritiqueIssue,
    DesignReviewAgent,
    SpecCritique,
    _coerce_critique,
    format_prior_critiques,
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
        lambda *_a, **_k: object(),
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


def test_format_prior_critiques_empty_is_none_yet() -> None:
    """Empty / ``None`` lineage renders the sentinel, not a blank block."""
    assert format_prior_critiques(None) == "None yet."
    assert format_prior_critiques([]) == "None yet."


def test_format_prior_critiques_renders_per_issue_detail() -> None:
    """Each prior critique renders a header line plus one indented line per
    issue carrying severity, field, description, and suggested_fix — so a
    later revision can see *what* an earlier round fixed (terse rationale and
    all) and avoid regressing it."""
    prior = [
        SpecCritique(
            ready=False,
            rationale="terse",
            round=0,
            issues=[
                CritiqueIssue(
                    field="sizing",
                    severity="critical",
                    description="position too large",
                    suggested_fix="cap fixed_fraction at 0.01",
                ),
                # An issue with no suggested_fix omits the "(fix: ...)" suffix.
                CritiqueIssue(field="exit_rules", severity="warning", description="no stop"),
            ],
        )
    ]

    rendered = format_prior_critiques(prior)

    assert "Round 0: ready=False (2 issues) — terse" in rendered
    assert "- [critical] sizing: position too large (fix: cap fixed_fraction at 0.01)" in rendered
    assert "- [warning] exit_rules: no stop" in rendered
    # The fix-less issue must not render an empty "(fix: )" suffix.
    assert "no stop (fix:" not in rendered


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


# ---------------------------------------------------------------------------
# Strict-bool coercion and ready/issues-contradiction demotion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ready_value", ["false", "False", "FALSE", "no", "", 0, 1, "yes", None])
def test_ready_non_bool_values_default_to_false(
    monkeypatch: pytest.MonkeyPatch, ready_value
) -> None:
    """``ready`` is only honoured when it's a real ``bool`` or the literal
    strings ``"true"`` / ``"false"``. Everything else fails closed."""
    payload = json.dumps({"ready": ready_value, "rationale": "ambiguous", "issues": []})
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False


def test_ready_true_string_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case-insensitive literal ``"true"`` (any case) is accepted."""
    payload = json.dumps({"ready": "TRUE", "rationale": "ok", "issues": []})
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True


def test_ready_true_with_critical_issue_is_demoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ready=True`` alongside a critical issue is self-contradictory; the
    coercer demotes to ``ready=False`` and appends an audit-trail issue so
    the design loop keeps iterating rather than advancing on the verdict."""
    payload = json.dumps(
        {
            "ready": True,
            "rationale": "ok",
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
    # Original critical issue is preserved AND an audit issue is appended.
    assert len(critique.issues) == 2
    assert critique.issues[0].severity == "critical"
    assert any(
        "demoting" in i.description and "ready=true" in i.description.lower()
        for i in critique.issues
    )


def test_ready_true_with_warning_issue_is_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demotion applies to any non-info severity, not just critical."""
    payload = json.dumps(
        {
            "ready": True,
            "rationale": "passing with concerns",
            "issues": [
                {
                    "field": "exit_rules",
                    "severity": "warning",
                    "description": "exit leg looks thin",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False


def test_ready_true_with_info_only_issues_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Info-only issues are advisory; they do not contradict ``ready=True``
    and the verdict survives unchanged."""
    payload = json.dumps(
        {
            "ready": True,
            "rationale": "looks fine",
            "issues": [
                {
                    "field": "hypothesis",
                    "severity": "info",
                    "description": "FYI: thesis aligns with prior winner",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert len(critique.issues) == 1
    assert critique.issues[0].severity == "info"
    assert "thesis aligns with prior winner" in critique.issues[0].description


# ---------------------------------------------------------------------------
# _coerce_critique demotion threshold — the self-review path passes
# demote_min_severity="critical" so advisory warnings on a ready=true verdict
# are accepted instead of burning a self-revision round.
# ---------------------------------------------------------------------------


def test_coerce_critique_critical_threshold_keeps_ready_true_with_warning() -> None:
    """With ``demote_min_severity="critical"`` a ready=true + warning verdict is
    accepted verbatim — no demotion, no synthetic audit issue appended."""
    parsed = {
        "ready": True,
        "rationale": "fine, minor note",
        "issues": [{"field": "exit_rules", "severity": "warning", "description": "thin"}],
    }

    critique = _coerce_critique(parsed, [], demote_min_severity="critical")

    assert critique.ready is True
    assert len(critique.issues) == 1
    assert critique.issues[0].severity == "warning"


def test_coerce_critique_critical_threshold_keeps_ready_true_with_info() -> None:
    """Info-only issues never demote at any threshold."""
    parsed = {
        "ready": True,
        "rationale": "fine",
        "issues": [{"field": "hypothesis", "severity": "info", "description": "fyi"}],
    }

    critique = _coerce_critique(parsed, [], demote_min_severity="critical")

    assert critique.ready is True
    assert len(critique.issues) == 1
    assert critique.issues[0].severity == "info"


def test_coerce_critique_critical_threshold_still_demotes_critical() -> None:
    """A ready=true + critical verdict is a real contradiction and is demoted
    even under the lenient self-review threshold, with an audit issue appended."""
    parsed = {
        "ready": True,
        "rationale": "claims ready",
        "issues": [
            {"field": "entry_rules", "severity": "critical", "description": "no adx predicate"}
        ],
    }

    critique = _coerce_critique(parsed, [], demote_min_severity="critical")

    assert critique.ready is False
    assert len(critique.issues) == 2
    assert critique.issues[0].severity == "critical"
    assert any("demoting" in i.description for i in critique.issues)


def test_coerce_critique_default_threshold_demotes_warning() -> None:
    """The default threshold (external reviewer path) still demotes on warning."""
    parsed = {
        "ready": True,
        "rationale": "passing with concerns",
        "issues": [{"field": "exit_rules", "severity": "warning", "description": "thin"}],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False


# ---------------------------------------------------------------------------
# not-ready critiques must always carry a blocking open issue, so the
# CritiqueLedger / stall detector and telemetry stay consistent with the
# loop's ready=False revise behaviour.
# ---------------------------------------------------------------------------


def test_coerce_critique_not_ready_info_only_gets_blocking_placeholder() -> None:
    """A ``ready=false`` verdict carrying only ``info`` issues would present an
    empty open set (the loop keeps revising on ready=false, but the ledger /
    stall detector track only blocking issues). Coercion must synthesise a
    blocking placeholder so ``open_issue_ids`` is non-empty, while keeping the
    advisory info note."""
    parsed = {
        "ready": False,
        "rationale": "not ready, minor note only",
        "issues": [{"field": "sizing", "severity": "info", "description": "fyi"}],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False
    # Open set (warnings + criticals) must be non-empty so stall detection and
    # telemetry reflect that the loop has blocking work to do.
    assert critique.open_issue_ids, critique.issues
    # The original info note is preserved (advisory, not in the open set).
    severities = sorted(i.severity for i in critique.issues)
    assert "info" in severities
    assert "warning" in severities


def test_coerce_critique_not_ready_with_warning_unchanged() -> None:
    """A ``ready=false`` verdict that already names a blocking issue is left
    alone — no extra synthetic placeholder is appended."""
    parsed = {
        "ready": False,
        "rationale": "needs work",
        "issues": [{"field": "exit_rules", "severity": "warning", "description": "add tp"}],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False
    assert len(critique.issues) == 1
    assert critique.open_issue_ids == {critique.issues[0].issue_id}


def test_coerce_critique_not_ready_no_issues_still_gets_placeholder() -> None:
    """The pre-existing zero-issue not-ready contract still holds."""
    parsed = {"ready": False, "rationale": "vague", "issues": []}

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False
    assert len(critique.open_issue_ids) == 1


# ---------------------------------------------------------------------------
# Deterministic-gate carve-out: the LLM reviewer may not block on sizing /
# risk_limits (the deterministic SpecReadinessGate owns that math and has
# already passed), and max drawdown is not a constraint at all. Such issues are
# demoted to info, and a not-ready verdict whose ONLY blocking objections were
# these is promoted to ready.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["sizing", "risk_limits"])
def test_coerce_critique_demotes_owned_field_to_info(field: str) -> None:
    """A blocking sizing/risk_limits issue is demoted to ``info`` so it can never
    keep an otherwise-ready verdict from advancing."""
    parsed = {
        "ready": True,
        "rationale": "fine",
        "issues": [{"field": field, "severity": "critical", "description": "tight"}],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is True
    assert len(critique.issues) == 1
    assert critique.issues[0].severity == "info"
    # Demoted to info ⇒ not a blocking open issue.
    assert critique.open_issue_ids == set()


def test_coerce_critique_not_ready_only_sizing_is_promoted_to_ready() -> None:
    """The recurring failure mode: the reviewer says not-ready solely because of
    a sizing/drawdown objection (e.g. the deployed-size-vs-stop '0.25% per trade'
    misread or 'max drawdown unreachable'). With those demoted, there is nothing
    blockable left, so the verdict is promoted to ready rather than churning the
    design loop to the round cap."""
    parsed = {
        "ready": False,
        "rationale": (
            "sizing 'risk 5% per trade' is 0.25% with the stop, and the 20% max "
            "drawdown limit is unreachable"
        ),
        "issues": [
            {
                "field": "sizing",
                "severity": "critical",
                "description": "risk 5% per trade is 0.25% per trade with the 5% stop",
            },
            {
                "field": "risk_limits",
                "severity": "critical",
                "description": "20% max drawdown limit is unreachable by design",
            },
        ],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is True
    # No blocking open issues remain — both objections were on owned fields.
    assert critique.open_issue_ids == set()
    # An audit-trail info note records the override.
    assert all(i.severity == "info" for i in critique.issues)
    assert any("deterministic readiness gate owns" in i.description for i in critique.issues)


def test_coerce_critique_not_ready_sizing_plus_real_defect_stays_not_ready() -> None:
    """A genuine non-owned defect (thesis/signal/etc.) still blocks even when a
    sizing objection rides alongside it — only the sizing issue is neutralised."""
    parsed = {
        "ready": False,
        "rationale": "mean-reversion entry on a momentum thesis, and sizing is tight",
        "issues": [
            {
                "field": "entry_rules",
                "severity": "critical",
                "description": "entry contradicts the momentum hypothesis",
            },
            {"field": "sizing", "severity": "critical", "description": "fraction tight"},
        ],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False
    # The entry_rules critical still blocks; the sizing issue is demoted to info.
    blocking = [i for i in critique.issues if i.severity in ("warning", "critical")]
    assert len(blocking) == 1
    assert blocking[0].field == "entry_rules"
    assert any(i.field == "sizing" and i.severity == "info" for i in critique.issues)
