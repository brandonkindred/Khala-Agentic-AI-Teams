"""Tests for sales team Agent Console manifests.

Validates that every manifest YAML parses, has resolvable schema_refs,
and has an importable entrypoint. CI-friendly: no LLM, no Strands, no
Postgres required.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from agent_platform.registry.models import AgentManifest
from agent_platform.registry.schema_resolver import resolve_schema

MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "agent_console" / "manifests"
EXPECTED_COUNT = 10
TEAM_KEY = "sales_team"


def _load_all_manifests() -> list[tuple[Path, AgentManifest]]:
    files = sorted(MANIFESTS_DIR.glob("*.yaml"))
    results = []
    for f in files:
        raw = yaml.safe_load(f.read_text())
        manifest = AgentManifest.model_validate(raw)
        results.append((f, manifest))
    return results


@pytest.fixture(scope="module")
def manifests() -> list[tuple[Path, AgentManifest]]:
    return _load_all_manifests()


def test_expected_manifest_count(manifests):
    assert len(manifests) == EXPECTED_COUNT, (
        f"Expected {EXPECTED_COUNT} manifests, found {len(manifests)}"
    )


def test_all_manifests_use_correct_team(manifests):
    for path, m in manifests:
        assert m.team == TEAM_KEY, f"{path.name}: team={m.team!r}, expected {TEAM_KEY!r}"


def test_no_duplicate_ids(manifests):
    ids = [m.id for _, m in manifests]
    assert len(ids) == len(set(ids)), f"Duplicate manifest ids: {ids}"


def test_all_ids_start_with_sales(manifests):
    for path, m in manifests:
        assert m.id.startswith("sales."), f"{path.name}: id={m.id!r} should start with 'sales.'"


@pytest.mark.parametrize("idx", range(EXPECTED_COUNT))
def test_input_schema_resolves(manifests, idx):
    _, m = manifests[idx]
    if m.inputs and m.inputs.schema_ref:
        schema = resolve_schema(m.inputs.schema_ref)
        assert isinstance(schema, dict), f"{m.id}: inputs.schema_ref did not resolve to a dict"


@pytest.mark.parametrize("idx", range(EXPECTED_COUNT))
def test_output_schema_resolves(manifests, idx):
    _, m = manifests[idx]
    if m.outputs and m.outputs.schema_ref:
        schema = resolve_schema(m.outputs.schema_ref)
        assert isinstance(schema, dict), f"{m.id}: outputs.schema_ref did not resolve to a dict"


@pytest.mark.parametrize("idx", range(EXPECTED_COUNT))
def test_entrypoint_importable(manifests, idx):
    _, m = manifests[idx]
    ep = m.source.entrypoint
    module_path, symbol = ep.split(":", 1)
    mod = importlib.import_module(module_path)
    target = getattr(mod, symbol, None)
    assert target is not None, f"{m.id}: {module_path} has no attribute {symbol!r}"
    assert callable(target), f"{m.id}: {ep} is not callable"


def test_dossier_builder_has_live_integration_tag(manifests):
    dossier = next((m for _, m in manifests if "dossier" in m.id), None)
    assert dossier is not None, "No dossier_builder manifest found"
    assert "requires-live-integration" in dossier.tags


def test_non_dossier_agents_lack_live_integration_tag(manifests):
    for path, m in manifests:
        if "dossier" not in m.id:
            assert "requires-live-integration" not in m.tags, (
                f"{path.name} unexpectedly tagged with requires-live-integration"
            )
