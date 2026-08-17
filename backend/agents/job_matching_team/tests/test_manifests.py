"""Validate the Job Matching Agent Console manifests.

No LLM, Strands, or Postgres required: every manifest must parse, expose
resolvable schema_refs, and have an importable entrypoint.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from agent_platform.registry.models import AgentManifest
from agent_platform.registry.schema_resolver import resolve_schema

MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "agent_console" / "manifests"
EXPECTED_COUNT = 2
TEAM_KEY = "job_matching"


def _load_all_manifests() -> list[tuple[Path, AgentManifest]]:
    results = []
    for f in sorted(MANIFESTS_DIR.glob("*.yaml")):
        results.append((f, AgentManifest.model_validate(yaml.safe_load(f.read_text()))))
    return results


@pytest.fixture(scope="module")
def manifests() -> list[tuple[Path, AgentManifest]]:
    return _load_all_manifests()


def test_expected_manifest_count(manifests):
    assert len(manifests) == EXPECTED_COUNT


def test_all_manifests_use_correct_team(manifests):
    for path, m in manifests:
        assert m.team == TEAM_KEY, f"{path.name}: team={m.team!r}"


def test_ids_unique_and_namespaced(manifests):
    ids = [m.id for _, m in manifests]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("job_matching.") for i in ids)


@pytest.mark.parametrize("idx", range(EXPECTED_COUNT))
def test_schema_refs_resolve(manifests, idx):
    _, m = manifests[idx]
    assert isinstance(resolve_schema(m.inputs.schema_ref), dict)
    assert isinstance(resolve_schema(m.outputs.schema_ref), dict)


@pytest.mark.parametrize("idx", range(EXPECTED_COUNT))
def test_entrypoints_importable(manifests, idx):
    _, m = manifests[idx]
    module_path, symbol = m.source.entrypoint.split(":", 1)
    target = getattr(importlib.import_module(module_path), symbol, None)
    assert callable(target), f"{m.id}: {m.source.entrypoint} not callable"


def test_scanner_tagged_live_integration(manifests):
    scanner = next(m for _, m in manifests if m.id == "job_matching.scanner")
    assert "requires-live-integration" in scanner.tags


def test_ranker_not_tagged_live_integration(manifests):
    ranker = next(m for _, m in manifests if m.id == "job_matching.ranker")
    assert "requires-live-integration" not in ranker.tags
