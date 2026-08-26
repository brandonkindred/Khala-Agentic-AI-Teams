"""Sync-check: every Strategy Lab wire model's fields are a subset of its
persisted counterpart's, modulo a documented exclusion list.

Applies ``assert_wire_fields_subset`` to all 7 wire-model / persisted-model
pairs in ``_response_schemas.py``, plus the nested ``_AlignmentIssueWire`` /
``AlignmentIssue`` pair used inside ``_AlignmentFixWire.issues``. This is the
standing CI guard against a wire model silently outgrowing its persisted
counterpart.
"""

from __future__ import annotations

import pytest

from investment_team.models import ExpectancyForecast, StrategySpec
from investment_team.strategy_lab.agents._response_schemas import (
    _AlignmentFixWire,
    _AlignmentIssueWire,
    _CritiqueIssueWire,
    _CritiqueWire,
    _DesignSpecWire,
    _ExpectancyForecastWire,
    _RefinementWire,
    _ZeroTradeRepairWire,
)
from investment_team.strategy_lab.agents.alignment import AlignmentIssue, TradeAlignmentReport
from investment_team.strategy_lab.agents.design_review import CritiqueIssue, SpecCritique
from investment_team.strategy_lab.agents.zero_trade_repair import ZeroTradeRepairReport

from ._wire_model_sync_test_helpers import assert_wire_fields_subset

# Each entry: (wire model, persisted model, exclusions). Exclusions are
# wire-only fields that never round-trip onto the persisted model, each with
# a one-line reason in the trailing comment. Fields present only on the
# persisted side never need an entry here — the check is one-directional
# (wire subset-of persisted).
_PAIRS = [
    (
        _ExpectancyForecastWire,
        ExpectancyForecast,
        (),  # exact field match
    ),
    (
        _DesignSpecWire,
        StrategySpec,
        (
            "rationale",  # popped off the parsed dict in design.py and returned
            # separately as (strategy_dict, rationale); never merged onto StrategySpec
        ),
    ),
    (
        _CritiqueIssueWire,
        CritiqueIssue,
        (
            "issue_id",  # deterministic identity computed by compute_issue_id,
            # never emitted by the LLM (persisted-only in practice)
        ),
    ),
    (
        _CritiqueWire,
        SpecCritique,
        (
            "readiness_findings",  # deterministic SpecReadinessGate snapshot the
            # orchestrator attaches for the audit trail, never asked of the LLM
            "round",  # review-round counter the orchestrator tracks, never asked of the LLM
        ),
    ),
    (
        _RefinementWire,
        StrategySpec,
        (
            "changes_made",  # 1-2 sentence LLM summary logged for audit / drift-collector
            # labels (orchestrator._apply_updates); never written onto the spec
            # (refinement.py's _ALLOWED_OUTPUT_KEYS confirms it's the only key narrowed out)
        ),
    ),
    (
        _ZeroTradeRepairWire,
        ZeroTradeRepairReport,
        (
            "dropped_spec_update_keys",  # populated by _coerce_report after the LLM
            # responds; never emitted by the LLM itself
        ),
    ),
    (
        _AlignmentFixWire,
        TradeAlignmentReport,
        (
            "alignment_findings",  # the orchestrator re-attaches the deterministic
            # ledger; the LLM never authors it
        ),
    ),
    (
        # Nested item type inside _AlignmentFixWire.issues — not one of the 7 named
        # pairs, but the same drift risk applies, so it's covered here too.
        _AlignmentIssueWire,
        AlignmentIssue,
        (),  # exact field match
    ),
]

_PAIR_IDS = [f"{wire.__name__}->{persisted.__name__}" for wire, persisted, _ in _PAIRS]


@pytest.mark.parametrize(("wire_cls", "persisted_cls", "exclusions"), _PAIRS, ids=_PAIR_IDS)
def test_wire_model_fields_are_subset_of_persisted_model(
    wire_cls, persisted_cls, exclusions
) -> None:
    assert_wire_fields_subset(wire_cls, persisted_cls, exclusions=exclusions)
