"""Canonical JSON-Schema definitions for the Strategy Lab agents' LLM output.

The Strategy Lab spec-authoring agents (design, design-review, refinement,
zero-trade repair, alignment fix-proposer) emit JSON that is recovered with a
brace-counting extractor and validated with pydantic. The Ollama transport
routes through the ``llm_service`` client in ``json_object`` wire mode (see
``model_factory.get_strands_model``), which forces a JSON object on the wire but
not a specific shape; the shape contract is enforced by pydantic validation
downstream. This ``json_object`` + pydantic path is the sole contract for the
zero-trade-repair and alignment agents, and the graceful-degradation fallback
for every agent when provider-enforced decoding is unavailable.

``RefinementAgent``, ``DesignAgent``, and ``DesignReviewAgent`` additionally
request provider-enforced, decoder-level schema-conformant decoding for their
respective schemas (``REFINEMENT_SCHEMA``, ``DESIGN_SPEC_SCHEMA``,
``CRITIQUE_SCHEMA``) via ``LLMClient.complete_json(schema=...)`` whenever
``llm_service.provider_supports_structured_output(...)`` is True (Ollama only
today), which removes the happy-path parse-correction resend on these
token-heavy calls. This is capability-gated per call site and degrades to the
``json_object`` + pydantic path above on the ``schema_forced`` starvation
signal — the same failure that retired the *unconditional* decoder-level
``format=<json-schema>`` constraint, which (paired with thinking-enabled models
on long code-emitting turns) could starve the content channel and yield an
empty, brace-less response. See ``strategy_lab/README.md`` (§ *Ollama LLM
transport*) for the full behavior and telemetry narrative.

These constants remain the canonical machine-readable definition of each
agent's wire shape: the refinement agent embeds ``REFINEMENT_SCHEMA`` verbatim
in its prompt (``refinement._REFINEMENT_SCHEMA_JSON``), and all five are
validated for well-formedness by the test-suite. Each is derived from a *wire
model* that mirrors the shape the agent asks the LLM to emit (not the richer
persisted model — e.g. the designer emits ``rationale`` and never
``strategy_id`` or ``audit``). Rule-shaped fields reuse the structured DSL types
from ``spec_dsl`` so the schema documents the entry/exit/sizing contract.

Schemas are computed once at import and exposed as module-level constants.

Invariants:
  * Each ``*_SCHEMA`` constant is a JSON-serializable JSON-Schema ``dict``.
  * The wire models track the corresponding prompt template's documented
    output shape; when a prompt changes, update the matching wire model here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..spec_dsl import EntryRule, ExitRule, SizingRule

# ---------------------------------------------------------------------------
# Wire models — one per agent, mirroring the JSON each prompt asks for.
# ---------------------------------------------------------------------------


class _ExpectancyForecastWire(BaseModel):
    """Wire shape of the designer's pre-commit expectancy forecast.

    Mirrors ``models.ExpectancyForecast`` (the persisted form, which adds
    clamping validators). Kept local here so the schema module stays decoupled
    from the heavier models module — same rationale as ``risk_limits`` below.
    Keep the field set in sync with ``models.ExpectancyForecast`` and the
    ``expectancy_forecast`` block documented in ``_DESIGN_USER_TEMPLATE`` /
    ``design_system.md``.
    """

    forecast_win_rate: float = 0.0
    reward_risk: float = 0.0
    trades_per_year: float = 0.0
    projected_annual_return_pct: float = 0.0
    consistency_note: str = ""


class _DesignSpecWire(BaseModel):
    """Shape emitted by :class:`DesignAgent` (``run`` / ``revise``).

    Mirrors ``design_system.md`` + ``_DESIGN_USER_TEMPLATE``: the structured
    spec plus the ``rationale`` and ``expectancy_forecast`` the designer
    returns alongside it. Excludes fields the orchestrator owns
    (``strategy_id``, ``audit``, ``requires_custom_code`` …) and the
    ``strategy_code`` key the designer is explicitly told not to emit.
    """

    asset_class: str
    hypothesis: str
    signal_definition: str
    timeframe: Literal["1m", "5m", "15m", "1h", "1d"]
    entry_rules: List[EntryRule] = Field(default_factory=list)
    exit_rules: List[ExitRule] = Field(default_factory=list)
    sizing: SizingRule
    target_symbols: List[str] = Field(default_factory=list)
    # The persisted model validates risk_limits into a typed RiskLimits; the
    # wire shape is a free object (the prompt shows ``{"max_position_pct": 5,
    # "stop_loss_pct": 3}``) so the schema stays decoupled from the heavier
    # models module and tolerant of the partial dict the LLM emits.
    risk_limits: Dict[str, Any] = Field(default_factory=dict)
    speculative: bool = False
    rationale: str = ""
    # Advisory expectancy forecast (win rate / reward:risk / trades-per-year /
    # projected return). Optional — the designer is asked for it but it is
    # never gated, so the wire shape tolerates its absence.
    expectancy_forecast: Optional[_ExpectancyForecastWire] = None


class _CritiqueIssueWire(BaseModel):
    """One reviewer issue — mirrors ``CritiqueIssue`` at the prompt level."""

    field: str
    severity: Literal["info", "warning", "critical"] = "warning"
    description: str
    suggested_fix: str = ""


class _CritiqueWire(BaseModel):
    """Shape emitted by :class:`DesignReviewAgent` and the design self-review.

    Mirrors ``_REVIEW_USER_TEMPLATE`` (and ``design_self_review_system.md``):
    ``{ready, rationale, issues}``. The persisted ``SpecCritique`` adds
    ``readiness_findings`` / ``round`` which the orchestrator fills in — the
    LLM is never asked for them.
    """

    ready: bool
    rationale: str = ""
    issues: List[_CritiqueIssueWire] = Field(default_factory=list)


class _RefinementWire(BaseModel):
    """Shape emitted by :class:`RefinementAgent` — code-only output.

    Mirrors ``_REFINEMENT_USER_TEMPLATE``: the fixed code plus a one-line
    summary. ``risk_limits`` is an optional tighten-only passthrough the
    orchestrator may honour.
    """

    strategy_code: str
    changes_made: str = ""
    risk_limits: Optional[Dict[str, Any]] = None


class _ZeroTradeRepairWire(BaseModel):
    """Shape emitted by :class:`ZeroTradeRepairAgent`.

    Mirrors ``zero_trade_repair_system.md`` + ``_ZERO_TRADE_USER_TEMPLATE``.
    Excludes ``dropped_spec_update_keys`` (populated by ``_coerce_report``,
    never emitted by the LLM).
    """

    # Mirrors ``models.ZeroTradeCategory``. Kept local here so the schema
    # module stays decoupled from the heavier models module — same rationale
    # as ``risk_limits`` in ``_DesignSpecWire`` above. Keep this literal's
    # members in sync with ``models.ZeroTradeCategory``; a test asserts the
    # two stay equal.
    root_cause_category: Literal[
        "NO_ORDERS_EMITTED",
        "ALL_ENTRIES_RISK_CAPPED",
        "ONLY_WARMUP_ORDERS",
        "ORDERS_REJECTED",
        "ORDERS_UNFILLED",
        "ENTRY_WITH_NO_EXIT",
        "UNKNOWN_ZERO_TRADE_PATH",
    ]
    evidence: str = ""
    code_issue: Optional[str] = None
    strategy_rule_issue: Optional[str] = None
    proposed_code: Optional[str] = None
    expected_order_count_change: int = 0
    expected_trade_count_change: int = 0
    changes_made: str = ""
    proposed_spec_updates: Optional[Dict[str, Any]] = None


class _AlignmentIssueWire(BaseModel):
    """One alignment divergence — mirrors ``AlignmentIssue`` at prompt level."""

    rule_type: str
    description: str
    severity: Literal["info", "warning", "critical"] = "warning"
    affected_trades: List[int] = Field(default_factory=list)


class _AlignmentFixWire(BaseModel):
    """Shape emitted by :meth:`TradeAlignmentAgent.propose_code_fix`.

    Mirrors ``alignment_propose_fix.md``. Excludes ``alignment_findings``
    (the orchestrator re-attaches the deterministic ledger; the LLM does not
    author it).
    """

    aligned: bool
    rationale: str = ""
    issues: List[_AlignmentIssueWire] = Field(default_factory=list)
    proposed_code: Optional[str] = None
    predicted_aligned_after_fix: bool = False
    changes_made: str = ""


# ---------------------------------------------------------------------------
# Cached schema constants (computed once at import).
# ---------------------------------------------------------------------------

DESIGN_SPEC_SCHEMA: Dict[str, Any] = _DesignSpecWire.model_json_schema()
CRITIQUE_SCHEMA: Dict[str, Any] = _CritiqueWire.model_json_schema()
REFINEMENT_SCHEMA: Dict[str, Any] = _RefinementWire.model_json_schema()
ZERO_TRADE_REPAIR_SCHEMA: Dict[str, Any] = _ZeroTradeRepairWire.model_json_schema()
ALIGNMENT_FIX_SCHEMA: Dict[str, Any] = _AlignmentFixWire.model_json_schema()


__all__ = [
    "DESIGN_SPEC_SCHEMA",
    "CRITIQUE_SCHEMA",
    "REFINEMENT_SCHEMA",
    "ZERO_TRADE_REPAIR_SCHEMA",
    "ALIGNMENT_FIX_SCHEMA",
]
