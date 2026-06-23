"""Tests for the Strategy Lab agents' JSON-Schema wire definitions.

Covers:
  * the cached wire schemas are well-formed JSON-Schema dicts;
  * the DesignAgent malformed-JSON path retries (instead of aborting the
    cycle) and is gated by ``STRATEGY_LAB_DESIGN_PARSE_RETRIES``.

The Ollama transport now routes through the ``llm_service`` adapter in
``json_object`` wire mode (the schemas are no longer forwarded to a decoder
``format`` constraint), so the routing behaviour is covered in
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
