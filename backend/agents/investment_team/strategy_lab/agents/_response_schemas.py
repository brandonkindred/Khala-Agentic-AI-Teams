"""Cached JSON-Schema providers for schema-constrained LLM decoding.

The Strategy Lab spec-authoring agents (design, design-review, refinement,
zero-trade repair, alignment fix-proposer) emit JSON that is then recovered
with a brace-counting extractor and validated with pydantic. When the
underlying model supports structured outputs (Ollama ``format=<json-schema>``),
passing the exact wire schema constrains the decoder so it can only emit
conforming JSON — eliminating the malformed-JSON and schema-drift failure
classes at the source. Pydantic validation downstream stays as
defense-in-depth.

This module derives one schema per agent from a *wire model* that mirrors the
shape each agent actually asks the LLM to emit (not the richer persisted
model — e.g. the designer emits ``rationale`` and never ``strategy_id`` or
``audit``). Rule-shaped fields reuse the structured DSL types from
``spec_dsl`` so the grammar enforces the entry/exit/sizing contract that the
prose correction preamble currently fights after the fact.

Schemas are computed once at import and exposed as module-level constants.

Invariants:
  * Each ``*_SCHEMA`` constant is a JSON-Schema ``dict`` suitable for the
    Ollama ``format`` field.
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


class _DesignSpecWire(BaseModel):
    """Shape emitted by :class:`DesignAgent` (``run`` / ``revise``).

    Mirrors ``design_system.md`` + ``_DESIGN_USER_TEMPLATE``: the structured
    spec plus the ``rationale`` the designer returns alongside it. Excludes
    fields the orchestrator owns (``strategy_id``, ``audit``,
    ``requires_custom_code`` …) and the ``strategy_code`` key the designer is
    explicitly told not to emit.
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

    root_cause_category: str
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
