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
from investment_team.strategy_lab.agents import _structured_output as so_mod
from investment_team.strategy_lab.agents._response_schemas import CRITIQUE_SCHEMA
from investment_team.strategy_lab.agents.design_review import (
    _CRITIQUE_SCHEMA_JSON,
    CritiqueIssue,
    DesignReviewAgent,
    SpecCritique,
    _coerce_critique,
    _sizing_owned_by_gate,
    format_prior_critiques,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    VolatilityTargetSizing,
)


@pytest.fixture(autouse=True)
def _force_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file exercises the unconstrained (non-structured) call path.

    Force the structured-output seam off so these tests are deterministic
    regardless of ambient ``LLM_PROVIDER`` (unset defaults to ``"ollama"``,
    whose capability flag is True) — see
    ``_structured_output.structured_output_available``. The structured path
    itself is covered by ``test_strategy_lab_design_review_structured_output.py``.
    """
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)


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


def test_format_prior_critiques_truncates_long_text() -> None:
    """Rationale, description, and suggested_fix are each truncated to the
    same shared preview length so a future change to the preview window
    can't update one site and drift from the others."""
    long_rationale = "R" * 200
    long_description = "D" * 200
    long_fix = "F" * 200
    prior = [
        SpecCritique(
            ready=False,
            rationale=long_rationale,
            round=0,
            issues=[
                CritiqueIssue(
                    field="sizing",
                    severity="warning",
                    description=long_description,
                    suggested_fix=long_fix,
                ),
            ],
        )
    ]

    rendered = format_prior_critiques(prior)

    assert ("R" * 160) in rendered
    assert ("R" * 161) not in rendered
    assert ("D" * 160) in rendered
    assert ("D" * 161) not in rendered
    assert ("F" * 160) in rendered
    assert ("F" * 161) not in rendered


def test_format_prior_critiques_short_lineage_unaffected_by_bounding() -> None:
    """len(prior) <= keep_last_n -> identical to unbounded rendering."""
    prior = [SpecCritique(ready=False, rationale=f"r{i}", round=i) for i in range(3)]
    rendered = format_prior_critiques(prior, keep_last_n=3)
    assert "earlier round(s) summarized" not in rendered
    for i in range(3):
        assert f"Round {i}: ready=False (0 issues) — r{i}" in rendered


def test_format_prior_critiques_over_cap_prepends_summary_and_keeps_last_n() -> None:
    prior = [SpecCritique(ready=False, rationale=f"r{i}", round=i) for i in range(8)]
    rendered = format_prior_critiques(prior, keep_last_n=3)
    lines = rendered.split("\n")
    assert "earlier round(s) summarized" in lines[0]
    # Only the last 3 critiques (rounds 5, 6, 7) are rendered in full, using
    # their real `round` field, not a renumbered index.
    assert "Round 5: ready=False (0 issues) — r5" in rendered
    assert "Round 6: ready=False (0 issues) — r6" in rendered
    assert "Round 7: ready=False (0 issues) — r7" in rendered
    for dropped in range(5):
        assert f"Round {dropped}: ready=False (0 issues) — r{dropped}" not in rendered


def test_format_prior_critiques_output_size_bounded_regardless_of_round_count() -> None:
    """Benchmark-style test: rendered length stays roughly constant as
    critique-lineage length grows, instead of growing linearly."""

    def _lineage(n: int) -> List[SpecCritique]:
        return [
            SpecCritique(
                ready=False,
                rationale="a fairly long rationale describing what went wrong" * 3,
                round=i,
                issues=[
                    CritiqueIssue(field="sizing", severity="warning", description="d" * 100),
                ],
            )
            for i in range(n)
        ]

    small = format_prior_critiques(_lineage(6), keep_last_n=5)
    large = format_prior_critiques(_lineage(500), keep_last_n=5)
    assert len(large) < len(small) * 3


def test_format_prior_critiques_summary_uses_actual_round_values_not_position() -> None:
    """Regression test: the design loop's `round` is 0-indexed
    (`orchestrator_design.py`'s `for review_round in range(max_rounds)`), so
    a naive positional "Round 1" label for the first *dropped* entry would
    collide with a kept critique whose real `round` is also 1. The summary
    must report the dropped entries' actual round numbers instead."""
    prior = [SpecCritique(ready=False, rationale=f"r{i}", round=i) for i in range(6)]
    rendered = format_prior_critiques(prior, keep_last_n=5)
    lines = rendered.split("\n")
    # Only round 0 is dropped (6 critiques, keep_last_n=5).
    assert "1 earlier round(s) summarized: Round 0 (0 issues): r0" in lines[0]
    # The ambiguous positional label from bound_history's generic summary
    # ("Round 1: ...") must not appear as a *kept*-style header for round 0.
    assert "Round 1: ready=False" not in lines[0]
    assert "Round 1: ready=False (0 issues) — r1" in rendered


def test_format_prior_critiques_summary_preserves_dropped_round_content() -> None:
    """The summary line for dropped rounds is not just a round range — it
    carries a short snippet (issue count, truncated rationale) per dropped
    round, so a fix noted in an older round's rationale isn't silently
    erased just because the round was dropped."""
    prior = [SpecCritique(ready=False, rationale="fixed the sizing overflow bug", round=0)] + [
        SpecCritique(ready=False, rationale=f"r{i}", round=i) for i in range(1, 6)
    ]
    rendered = format_prior_critiques(prior, keep_last_n=5)
    assert "fixed the sizing overflow bug" in rendered.split("\n")[0]


def test_format_prior_critiques_summary_stays_bounded_with_many_dropped_rounds() -> None:
    """Benchmark-style test for the dropped-round summary itself: even with
    hundreds of dropped rounds, each carrying a long rationale, the summary
    line stays capped instead of growing linearly."""
    prior = [
        SpecCritique(ready=False, rationale=f"a long rationale for round {i}" * 5, round=i)
        for i in range(500)
    ]
    rendered = format_prior_critiques(prior, keep_last_n=5)
    summary_line = rendered.split("\n")[0]
    # _CRITIQUE_SUMMARY_MAX_CHARS (240) plus the "  N earlier round(s) summarized: " prefix,
    # whose length varies only with len(str(N)) — bounded regardless of round count.
    assert len(summary_line) <= 300


def test_prompt_embeds_response_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The review prompt carries the JSON Schema so the wire model, the
    hand-written skeleton, and the downstream coercer cannot drift apart."""
    payload = json.dumps({"ready": True, "rationale": "fine", "issues": []})
    capture = _patch_review(monkeypatch, payload)

    DesignReviewAgent().run(_spec(), readiness_results=[])

    assert isinstance(capture, _CapturingAgent)
    prompt = capture.calls[0]
    assert "MUST conform to this JSON Schema" in prompt
    assert _CRITIQUE_SCHEMA_JSON in prompt


def test_embedded_schema_matches_format_constraint() -> None:
    """The schema embedded in the prompt is the same object exported from
    ``_response_schemas`` — the prompt-level contract cannot silently drift
    from whatever is validated elsewhere."""
    assert json.loads(_CRITIQUE_SCHEMA_JSON) == CRITIQUE_SCHEMA
    assert CRITIQUE_SCHEMA["required"] == ["ready"]
    assert {"ready", "rationale", "issues"} <= set(CRITIQUE_SCHEMA["properties"])


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


def test_non_iterable_issues_value_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-list ``issues`` value (e.g. schema drift emitting a bare
    scalar) must not crash the coercion — it's treated as no issues, same
    as an explicit ``[]``, so the not-ready placeholder path still fires."""
    payload = json.dumps({"ready": False, "rationale": "vibes", "issues": 42})
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert critique.issues
    assert "vibes" in critique.issues[0].description


def test_dict_shaped_issues_value_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single issue dict passed directly as ``issues`` (not wrapped in a
    list) is schema drift, not a list of issues — treated as no issues
    rather than silently iterating the dict's keys."""
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "vibes",
            "issues": {"field": "hypothesis", "description": "not a list"},
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert critique.issues
    assert "vibes" in critique.issues[0].description


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


class _ExplodingStr:
    """A value whose ``str()`` raises, to trigger a construction failure."""

    def __str__(self) -> str:
        raise ValueError("cannot stringify")


def test_one_bad_item_construction_failure_is_skipped() -> None:
    """A single item that fails ``CritiqueIssue`` construction (here, a
    description that can't stringify) is still best-effort-skipped by the
    narrowed exception clause, preserving the rest of the batch."""
    parsed = {
        "ready": False,
        "rationale": "mixed",
        "issues": [
            {"field": "hypothesis", "description": _ExplodingStr()},
            {
                "field": "exit_rules",
                "severity": "warning",
                "description": "no take_profit alongside stop_loss",
            },
        ],
    }

    critique = _coerce_critique(parsed, readiness_findings=[])

    assert len(critique.issues) == 1
    assert critique.issues[0].field == "exit_rules"


def test_unexpected_construction_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowing ``except Exception`` to ``(TypeError, ValueError,
    ValidationError)`` means a genuine programming error inside
    ``CritiqueIssue`` construction is no longer silently swallowed — it
    must propagate so regressions are visible instead of vanishing."""
    import investment_team.strategy_lab.agents.design_review as design_review_mod

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(design_review_mod, "CritiqueIssue", _boom)

    parsed = {
        "ready": False,
        "rationale": "mixed",
        "issues": [{"field": "hypothesis", "description": "x"}],
    }

    with pytest.raises(RuntimeError, match="unexpected bug"):
        _coerce_critique(parsed, readiness_findings=[])


# ---------------------------------------------------------------------------
# Strict-bool coercion and ready/issues-contradiction demotion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ready_value", ["no", "", 0, 1, "yes", None])
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


def test_ready_false_string_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case-insensitive literal ``"false"`` (any case) is accepted as ``False``."""
    payload = json.dumps({"ready": "FALSE", "rationale": "ok", "issues": []})
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False


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


def test_coerce_critique_accepts_expectancy_forecast_field() -> None:
    """``expectancy_forecast`` is a valid critique field (the objective-aware
    self-review check points at it) and must survive coercion rather than
    silently collapsing to the ``hypothesis`` fallback."""
    parsed = {
        "ready": False,
        "rationale": "win rate below break-even for the take-profit:stop geometry",
        "issues": [
            {
                "field": "expectancy_forecast",
                "severity": "critical",
                "description": "reward_risk 0.2 needs >83% wins; forecast is 60% — negative expectancy.",
                "suggested_fix": "Widen the take-profit or tighten the stop.",
            }
        ],
    }

    critique = _coerce_critique(parsed, [], demote_min_severity="critical")

    assert critique.ready is False
    assert any(i.field == "expectancy_forecast" for i in critique.issues)
    assert all(i.field != "hypothesis" for i in critique.issues)


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
# Deterministic-gate carve-out: the LLM reviewer may not block on sizing (the
# deterministic SpecReadinessGate owns that math and has already passed) or on
# any drawdown objection (max drawdown is not a constraint). Such issues are
# demoted to info, and a not-ready verdict whose ONLY blocking objections were
# these is promoted to ready. Non-drawdown risk_limits objections still block.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "description"),
    [
        ("sizing", "fraction looks tight"),
        ("risk_limits", "the 20% max drawdown limit is unreachable by design"),
        ("hypothesis", "thesis implies a max drawdown the sizing can't reach"),
    ],
)
def test_coerce_critique_demotes_sizing_and_drawdown_to_info(field: str, description: str) -> None:
    """A blocking ``sizing`` objection or any ``drawdown`` objection (whatever
    field) is demoted to ``info`` so it can never keep a ready verdict from
    advancing."""
    parsed = {
        "ready": True,
        "rationale": "fine",
        "issues": [{"field": field, "severity": "critical", "description": description}],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is True
    assert len(critique.issues) == 1
    assert critique.issues[0].severity == "info"
    # Demoted to info ⇒ not a blocking open issue.
    assert critique.open_issue_ids == set()


def test_coerce_critique_keeps_non_drawdown_risk_limit_blocking() -> None:
    """A genuine risk_limits defect the deterministic gate does NOT check —
    ``max_gross_leverage=0`` rejects every order at runtime — must keep blocking
    rather than be demoted and promoted (the deterministic gate only guards the
    zero/ceiling cases of ``max_position_pct``, not leverage)."""
    parsed = {
        "ready": False,
        "rationale": "leverage cap makes the strategy untradeable",
        "issues": [
            {
                "field": "risk_limits",
                "severity": "critical",
                "description": "max_gross_leverage=0 rejects every positive-notional order",
            }
        ],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False
    blocking = [i for i in critique.issues if i.severity in ("warning", "critical")]
    assert len(blocking) == 1
    assert blocking[0].field == "risk_limits"
    assert blocking[0].severity == "critical"


@pytest.mark.parametrize(
    "description",
    [
        # Genuine missing-exit defect that merely *mentions* drawdown — must NOT
        # be demoted just because the word appears.
        "strategy has neither a stop-loss nor a drawdown-protection exit",
        "no protective exit; an adverse move produces an unbounded drawdown",
    ],
)
def test_coerce_critique_keeps_defect_that_only_mentions_drawdown(description: str) -> None:
    """Only references to the retired max-drawdown *limit* are demoted. A
    substantive exit-completeness defect that merely contains the word
    "drawdown" keeps blocking."""
    parsed = {
        "ready": False,
        "rationale": "missing protective exit",
        "issues": [{"field": "exit_rules", "severity": "critical", "description": description}],
    }

    critique = _coerce_critique(parsed, [])

    assert critique.ready is False
    blocking = [i for i in critique.issues if i.severity in ("warning", "critical")]
    assert len(blocking) == 1
    assert blocking[0].field == "exit_rules"
    assert blocking[0].severity == "critical"


@pytest.mark.parametrize(
    ("kind", "owned"),
    [
        ("fixed_fraction", True),
        ("fixed_notional", True),
        ("volatility_target", False),
        (None, False),
        ("bogus", False),
    ],
)
def test_sizing_owned_by_gate(kind: object, owned: bool) -> None:
    """Only the static sizing kinds the deterministic gate fully validates are
    'owned'. volatility_target (gate abstains) and unknown/missing kinds are not."""
    assert _sizing_owned_by_gate(kind) is owned


def test_coerce_critique_volatility_sizing_objection_keeps_blocking() -> None:
    """When the spec's sizing kind is NOT gate-owned (volatility_target), a
    sizing objection must keep blocking — the deterministic gate abstains on it,
    so the reviewer's plausibility critique is the only substantive check."""
    parsed = {
        "ready": False,
        "rationale": "implausible vol target",
        "issues": [
            {
                "field": "sizing",
                "severity": "critical",
                "description": "target_annual_vol=0.001 is implausibly low and untradeable",
            }
        ],
    }

    critique = _coerce_critique(parsed, [], sizing_owned=False)

    assert critique.ready is False
    blocking = [i for i in critique.issues if i.severity in ("warning", "critical")]
    assert len(blocking) == 1
    assert blocking[0].field == "sizing"
    assert blocking[0].severity == "critical"


def test_design_review_run_keeps_volatility_target_sizing_objection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a volatility_target spec whose reviewer flags an implausible
    target keeps the verdict not-ready — ``run`` resolves ``sizing_owned`` from
    the spec's sizing kind, so the gate-abstained objection is not demoted."""
    spec = _spec().model_copy(update={"sizing": VolatilityTargetSizing(target_annual_vol=0.001)})
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "implausible vol target",
            "issues": [
                {
                    "field": "sizing",
                    "severity": "critical",
                    "description": "target_annual_vol=0.001 is implausibly low and untradeable",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(spec, readiness_results=[])

    assert critique.ready is False
    blocking = [i for i in critique.issues if i.severity in ("warning", "critical")]
    assert len(blocking) == 1
    assert blocking[0].field == "sizing"


def test_design_review_run_demotes_sizing_for_gate_owned_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end counterpart: for a gate-owned kind (fixed_fraction) a sole
    sizing objection IS demoted and the verdict promoted to ready."""
    spec = _spec().model_copy(update={"sizing": FixedFractionSizing(fraction=0.05)})
    payload = json.dumps(
        {
            "ready": False,
            "rationale": "risk 5% per trade is 0.25% with the stop",
            "issues": [
                {
                    "field": "sizing",
                    "severity": "critical",
                    "description": "risk 5% per trade is only 0.25% per trade with the 5% stop",
                }
            ],
        }
    )
    _patch_review(monkeypatch, payload)

    critique = DesignReviewAgent().run(spec, readiness_results=[])

    assert critique.ready is True
    assert all(i.severity == "info" for i in critique.issues)


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
