"""Tests for per-agent cognition scope resolution from the manifest."""

from __future__ import annotations

import types

from agent_cognition import manifest_scope


def _patch_registry(monkeypatch, manifest):
    """Patch ``agent_platform.registry.get_registry`` to return a registry yielding ``manifest``."""
    from agent_platform import registry

    fake = types.SimpleNamespace(get=lambda agent_id: manifest)
    monkeypatch.setattr(registry, "get_registry", lambda: fake)


def _manifest(
    *,
    enabled=True,
    ingest_events=True,
    ingest_summaries=True,
    ground=True,
    retention=90,
    rule_packs=(),
):
    kg = types.SimpleNamespace(
        enabled=enabled,
        ingest_events=ingest_events,
        ingest_summaries=ingest_summaries,
        ground_rule_proposals=ground,
    )
    memory = types.SimpleNamespace(retention_days_events=retention)
    cognition = types.SimpleNamespace(
        knowledge_graph=kg, memory=memory, rule_packs=list(rule_packs)
    )
    return types.SimpleNamespace(cognition=cognition)


# ---------------------------------------------------------------------------
# Defaults (no manifest / lookup failure)
# ---------------------------------------------------------------------------
def test_defaults_when_registry_raises(monkeypatch):
    from agent_platform import registry

    def _boom():
        raise RuntimeError("no registry")

    monkeypatch.setattr(registry, "get_registry", _boom)
    assert manifest_scope.graph_scope("a") == (True, True)
    assert manifest_scope.ground_rule_proposals("a") is True
    assert manifest_scope.retention_days("a") == 90


def test_defaults_when_manifest_missing(monkeypatch):
    _patch_registry(monkeypatch, None)
    assert manifest_scope.graph_scope("a") == (True, True)
    assert manifest_scope.ground_rule_proposals("a") is True
    assert manifest_scope.retention_days("a") == 90
    assert manifest_scope.rule_packs("a") == []


def test_rule_packs_empty_when_registry_raises(monkeypatch):
    from agent_platform import registry

    def _boom():
        raise RuntimeError("no registry")

    monkeypatch.setattr(registry, "get_registry", _boom)
    assert manifest_scope.rule_packs("a") == []


def test_defaults_when_no_cognition_block(monkeypatch):
    _patch_registry(monkeypatch, types.SimpleNamespace(cognition=None))
    assert manifest_scope.graph_scope("a") == (True, True)
    assert manifest_scope.ground_rule_proposals("a") is True


# ---------------------------------------------------------------------------
# Manifest-driven scope
# ---------------------------------------------------------------------------
def test_disabled_graph_ingests_nothing(monkeypatch):
    _patch_registry(monkeypatch, _manifest(enabled=False))
    assert manifest_scope.graph_scope("a") == (False, False)
    assert manifest_scope.ground_rule_proposals("a") is False


def test_per_kind_flags(monkeypatch):
    _patch_registry(monkeypatch, _manifest(ingest_events=False, ingest_summaries=True))
    assert manifest_scope.graph_scope("a") == (False, True)


def test_ground_flag_respected(monkeypatch):
    _patch_registry(monkeypatch, _manifest(ground=False))
    assert manifest_scope.ground_rule_proposals("a") is False


def test_retention_from_manifest(monkeypatch):
    _patch_registry(monkeypatch, _manifest(retention=30))
    assert manifest_scope.retention_days("a") == 30


def test_rule_packs_from_manifest(monkeypatch):
    _patch_registry(monkeypatch, _manifest(rule_packs=["default_guardrails", "extra"]))
    assert manifest_scope.rule_packs("a") == ["default_guardrails", "extra"]


def test_rule_packs_default_when_no_cognition_block(monkeypatch):
    _patch_registry(monkeypatch, types.SimpleNamespace(cognition=None))
    assert manifest_scope.rule_packs("a") == []
