"""Tests for schema-constrained (structured-output) decoding wiring.

Covers:
  * the cached wire schemas are well-formed JSON-Schema dicts;
  * each spec-authoring agent forwards its matching schema to
    ``get_strands_model`` at construction;
  * the DesignAgent malformed-JSON path now retries (instead of aborting
    the cycle) and is gated by ``STRATEGY_LAB_DESIGN_PARSE_RETRIES``.

The model-factory toggle / format-application behaviour lives in
``test_misc_helpers.py`` alongside the other ``get_strands_model`` tests.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from investment_team.strategy_lab.agents import _response_schemas as schemas

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        schemas.DESIGN_SPEC_SCHEMA,
        schemas.CRITIQUE_SCHEMA,
        schemas.REFINEMENT_SCHEMA,
        schemas.ZERO_TRADE_REPAIR_SCHEMA,
        schemas.ALIGNMENT_FIX_SCHEMA,
    ],
)
def test_schemas_are_serializable_object_schemas(schema: Dict[str, Any]) -> None:
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    assert "properties" in schema
    # Must round-trip through JSON for the Ollama ``format`` field.
    assert json.loads(json.dumps(schema)) == schema


def test_design_schema_carries_dsl_rule_defs() -> None:
    """The design schema must inline the structured DSL rule shapes so the
    decoder is grammar-constrained to the entry/exit/sizing contract."""
    props = schemas.DESIGN_SPEC_SCHEMA["properties"]
    assert {"entry_rules", "exit_rules", "sizing", "timeframe"} <= set(props)
    assert schemas.DESIGN_SPEC_SCHEMA["$defs"]  # rule variants resolved into $defs


def test_design_schema_excludes_orchestrator_owned_fields() -> None:
    """The designer never authors strategy_id/audit/strategy_code."""
    props = set(schemas.DESIGN_SPEC_SCHEMA["properties"])
    assert not ({"strategy_id", "audit", "strategy_code"} & props)


# ---------------------------------------------------------------------------
# Per-agent forwarding
# ---------------------------------------------------------------------------


class _SchemaRecorder:
    """Stand-in for ``get_strands_model`` that records the response_schema."""

    def __init__(self) -> None:
        self.schemas: List[Any] = []

    def __call__(self, _agent_key: str, *, timeout: Any = None, response_schema: Any = None):
        self.schemas.append(response_schema)
        return object()


class _StaticAgent:
    """Strands ``Agent`` replacement returning a fixed payload."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def __call__(self, _prompt: str) -> str:
        return self._payload


def test_design_review_agent_forwards_critique_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.models import RiskLimits, StrategySpec
    from investment_team.strategy_lab.agents import design_review as mod

    recorder = _SchemaRecorder()
    monkeypatch.setattr(mod, "get_strands_model", recorder)
    monkeypatch.setattr(
        mod, "Agent", lambda **_k: _StaticAgent(json.dumps({"ready": True, "rationale": "ok"}))
    )

    spec = StrategySpec(
        strategy_id="s1",
        authored_by="t",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )
    mod.DesignReviewAgent().run(spec)

    assert recorder.schemas == [schemas.CRITIQUE_SCHEMA]


def test_refinement_agent_forwards_refinement_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.models import RiskLimits, StrategySpec
    from investment_team.strategy_lab.agents import refinement as mod

    recorder = _SchemaRecorder()
    monkeypatch.setattr(mod, "get_strands_model", recorder)
    monkeypatch.setattr(
        mod,
        "Agent",
        lambda **_k: _StaticAgent(json.dumps({"strategy_code": "# fixed", "changes_made": "x"})),
    )

    spec = StrategySpec(
        strategy_id="s1",
        authored_by="t",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )
    mod.RefinementAgent().run(
        spec=spec, code="# old", failure_phase="execution", failure_details="boom"
    )

    assert recorder.schemas == [schemas.REFINEMENT_SCHEMA]


def test_design_agent_forwards_spec_then_critique_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Design generation uses the spec schema; the self-review pass uses the
    critique schema — proving the per-call schema threading."""
    from investment_team.strategy_lab.agents import design as mod

    recorder = _SchemaRecorder()
    monkeypatch.setattr(mod, "get_strands_model", recorder)

    spec_payload = json.dumps(
        {
            "asset_class": "stocks",
            "hypothesis": "h",
            "signal_definition": "s",
            "timeframe": "1d",
            "entry_rules": [],
            "exit_rules": [],
            "sizing": {"kind": "fixed_fraction", "fraction": 0.02},
            "target_symbols": [],
            "risk_limits": {"max_position_pct": 5},
            "rationale": "r",
        }
    )
    critique_payload = json.dumps({"ready": True, "rationale": "ok"})
    payloads = iter([spec_payload, critique_payload])
    monkeypatch.setattr(mod, "Agent", lambda **_k: _StaticAgent(next(payloads)))
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "true")

    mod.DesignAgent().run(prior_records=[])

    assert recorder.schemas == [schemas.DESIGN_SPEC_SCHEMA, schemas.CRITIQUE_SCHEMA]


# ---------------------------------------------------------------------------
# DesignAgent malformed-JSON retry (acceptance criterion 2)
# ---------------------------------------------------------------------------


class _ScriptedAgent:
    def __init__(self, payloads: List[str]) -> None:
        self._payloads = payloads
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return self._payloads[idx]


def _valid_spec_payload() -> str:
    return json.dumps(
        {
            "asset_class": "stocks",
            "hypothesis": "h",
            "signal_definition": "s",
            "timeframe": "1d",
            "entry_rules": [],
            "exit_rules": [],
            "sizing": {"kind": "fixed_fraction", "fraction": 0.02},
            "target_symbols": [],
            "risk_limits": {"max_position_pct": 5},
            "rationale": "r",
        }
    )


def test_design_agent_retries_malformed_json_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first unparseable response is re-prompted, not fatal, and the
    second (valid) response is accepted."""
    from investment_team.strategy_lab.agents import design as mod

    agent = _ScriptedAgent(["no json here", _valid_spec_payload()])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "2")

    parsed, rationale = mod.DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert rationale == "r"
    assert agent.calls == 2  # one malformed, one good


def test_design_agent_malformed_json_raises_after_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With retries exhausted, the malformed-JSON ValueError still surfaces."""
    from investment_team.strategy_lab.agents import design as mod

    agent = _ScriptedAgent(["never json"])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "0")

    with pytest.raises(ValueError):
        mod.DesignAgent().run(prior_records=[])
    assert agent.calls == 1
