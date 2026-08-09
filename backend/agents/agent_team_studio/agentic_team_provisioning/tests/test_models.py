"""Tests for the thin roster-ref schema (``AgenticTeamAgentRef``).

Covers construction defaults, the ``manifest_id``-required-for-registry
invariant, and JSON round-tripping — this model isn't wired into any
store/API/consumer yet, so these are bare model-level tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_team_studio.agentic_team_provisioning.models import (
    SOURCE_GENERATED,
    SOURCE_REGISTRY,
    AgenticTeamAgentRef,
)


def test_defaults_to_generated_with_no_manifest_id():
    ref = AgenticTeamAgentRef(agent_name="Planner")

    assert ref.agent_name == "Planner"
    assert ref.source == SOURCE_GENERATED
    assert ref.manifest_id is None


def test_registry_source_with_manifest_id_constructs():
    ref = AgenticTeamAgentRef(
        agent_name="Planner", source=SOURCE_REGISTRY, manifest_id="blogging.planner"
    )

    assert ref.source == SOURCE_REGISTRY
    assert ref.manifest_id == "blogging.planner"


@pytest.mark.parametrize("manifest_id", [None, ""])
def test_registry_source_without_manifest_id_raises(manifest_id):
    with pytest.raises(ValidationError, match="manifest_id is required"):
        AgenticTeamAgentRef(agent_name="Planner", source=SOURCE_REGISTRY, manifest_id=manifest_id)


def test_round_trips_through_json():
    ref = AgenticTeamAgentRef(
        agent_name="Planner", source=SOURCE_REGISTRY, manifest_id="blogging.planner"
    )

    dumped = ref.model_dump(mode="json")
    restored = AgenticTeamAgentRef.model_validate(dumped)

    assert restored == ref
