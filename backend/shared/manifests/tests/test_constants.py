"""Unit tests for generated-agent constants owned by :mod:`shared.manifests`."""

from __future__ import annotations

import shared.manifests as manifests
from agent_team_studio import manifest_shared
from shared.manifests import (
    AGENT_ANATOMY_REF,
    DEFAULT_RULE_PACKS,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    default_cognition_block,
)


def test_generated_agent_constants_have_canonical_values() -> None:
    assert GENERATED_AGENT_ENTRYPOINT == (
        "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
    )
    assert GENERATED_AGENT_INPUT_REF == ("agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeInput")
    assert GENERATED_AGENT_OUTPUT_REF == (
        "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeOutput"
    )
    assert AGENT_ANATOMY_REF == ("backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md")
    assert DEFAULT_RULE_PACKS == ("default_guardrails",)


def test_default_cognition_block_is_batteries_included() -> None:
    block = default_cognition_block()
    assert block.rule_packs == ["default_guardrails"]
    assert block.memory.retention_days_events == 90
    assert block.tools == []
    assert block.requires_idempotency_key is False
    assert block.knowledge_graph.enabled is True


def test_default_cognition_block_returns_a_fresh_instance_each_call() -> None:
    a = default_cognition_block()
    b = default_cognition_block()
    assert a is not b
    assert a == b


def test_package_docstring_documents_hashing_team_and_cognition_rules() -> None:
    doc = manifests.__doc__
    assert doc is not None
    lowered = doc.lower()
    assert "hash" in lowered
    assert "slug" in lowered
    assert "agent_studio" in doc
    assert "agentic_team_provisioning" in doc
    assert "cognition" in lowered
    assert "default_guardrails" in doc


def test_studio_shim_reexports_the_same_constant_objects() -> None:
    assert manifest_shared.GENERATED_AGENT_ENTRYPOINT is GENERATED_AGENT_ENTRYPOINT
    assert manifest_shared.GENERATED_AGENT_INPUT_REF is GENERATED_AGENT_INPUT_REF
    assert manifest_shared.GENERATED_AGENT_OUTPUT_REF is GENERATED_AGENT_OUTPUT_REF
    assert manifest_shared.AGENT_ANATOMY_REF is AGENT_ANATOMY_REF
    assert manifest_shared.DEFAULT_RULE_PACKS is DEFAULT_RULE_PACKS
    assert manifest_shared.default_cognition_block is default_cognition_block
