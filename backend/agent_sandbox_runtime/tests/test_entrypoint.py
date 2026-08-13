"""Tests for the single-agent sandbox bootstrap (issue #263).

Covers:
* ``SANDBOX_AGENT_ID`` required — missing env → clean ``SystemExit(2)``.
* Unknown agent id → ``SystemExit(3)`` (registry.get returns None, not raises).
* The single-agent guard middleware: only the bound agent id is invocable;
  same-team sibling agents get 404, not 200.
* ``/health`` returns the bound agent's metadata.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _dummy_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``LLM_PROVIDER=dummy`` so factories don't need a real Ollama."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    from llm_service import clear_client_cache

    clear_client_cache()


def _build_app(monkeypatch: pytest.MonkeyPatch, agent_id: str):
    """Fresh ``_build_app()`` call with the given ``SANDBOX_AGENT_ID``."""
    monkeypatch.setenv("SANDBOX_AGENT_ID", agent_id)
    from agent_sandbox_runtime.entrypoint import _build_app

    return _build_app()


def test_missing_sandbox_agent_id_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_AGENT_ID", raising=False)
    from agent_sandbox_runtime.entrypoint import EXIT_MISSING_ENV, _build_app

    with pytest.raises(SystemExit) as exc_info:
        _build_app()
    assert exc_info.value.code == EXIT_MISSING_ENV


def test_unknown_agent_id_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """registry.get() returns None for unknown ids — the bootstrap must still
    exit cleanly with ``EXIT_UNKNOWN_AGENT`` rather than AttributeError out."""
    monkeypatch.setenv("SANDBOX_AGENT_ID", "does.not.exist.anywhere")
    from agent_sandbox_runtime.entrypoint import EXIT_UNKNOWN_AGENT, _build_app

    with pytest.raises(SystemExit) as exc_info:
        _build_app()
    assert exc_info.value.code == EXIT_UNKNOWN_AGENT


def test_health_returns_bound_agent_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(monkeypatch, "blogging.planner")
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["agent_id"] == "blogging.planner"
    assert body["team"] == "blogging"


def test_invoke_bound_agent_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(monkeypatch, "blogging.planner")
    client = TestClient(app)

    body = {
        "brief": "Test brief about observability.",
        "research_digest": "## Sources\n- Source one: summary.",
        "length_policy_context": "Standard article, ~1000 words.",
    }
    resp = client.post("/_agents/blogging.planner/invoke", json=body)
    assert resp.status_code == 200, resp.text
    envelope = resp.json()
    assert envelope["error"] is None
    assert envelope["output"]["content_plan"]["requirements_analysis"]["plan_acceptable"] is True


def test_invoke_sibling_same_team_agent_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox bound to ``blogging.planner`` must not serve ``blogging.writer``.

    Without the single-agent guard, the shared shim's team-scoped check would
    accept this request and execute the wrong agent — the bug Codex flagged.
    """
    app = _build_app(monkeypatch, "blogging.planner")
    client = TestClient(app)

    resp = client.post(
        "/_agents/blogging.writer/invoke",
        json={"brief": "x", "research_digest": "x", "length_policy_context": "x"},
    )
    assert resp.status_code == 404
    assert "Sandbox is bound to 'blogging.planner'" in resp.text


def test_invoke_cross_team_agent_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-team requests are rejected by the single-agent guard first."""
    app = _build_app(monkeypatch, "blogging.planner")
    client = TestClient(app)

    resp = client.post("/_agents/branding.creative_director/invoke", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sandbox secrets loader (issue #257)
# ---------------------------------------------------------------------------


def test_secrets_loader_populates_environ_and_unlinks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Loader reads KEY=VALUE pairs into ``os.environ`` and unlinks the file."""
    from agent_sandbox_runtime.entrypoint import _load_sandbox_secrets

    secrets = tmp_path / "sandbox-env"
    secrets.write_text(
        "\n".join(
            [
                "OLLAMA_API_KEY=ollama-xyz",
                "POSTGRES_PASSWORD=pg-xyz",
                "# comment line",
                "",
                "POSTGRES_USER=sandbox_blogging",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANDBOX_SECRETS_FILE", str(secrets))
    # Make sure these aren't pre-set in the test process.
    for key in ("OLLAMA_API_KEY", "POSTGRES_PASSWORD", "POSTGRES_USER"):
        monkeypatch.delenv(key, raising=False)

    _load_sandbox_secrets()

    assert os.environ["OLLAMA_API_KEY"] == "ollama-xyz"
    assert os.environ["POSTGRES_PASSWORD"] == "pg-xyz"
    assert os.environ["POSTGRES_USER"] == "sandbox_blogging"
    # After loading, the in-sandbox view is unlinked so agent code can't cat it.
    assert not secrets.exists()


def test_secrets_loader_noop_when_env_marker_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``SANDBOX_SECRETS_FILE`` set → loader is a silent no-op.

    This keeps unit tests (and non-sandbox invocations) working unchanged.
    """
    from agent_sandbox_runtime.entrypoint import _load_sandbox_secrets

    monkeypatch.delenv("SANDBOX_SECRETS_FILE", raising=False)
    # Must not raise.
    _load_sandbox_secrets()


def test_secrets_loader_noop_when_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``SANDBOX_SECRETS_FILE`` pointing at a nonexistent file is a no-op too.

    Guards against races where the file was already unlinked by a prior call.
    """
    from agent_sandbox_runtime.entrypoint import _load_sandbox_secrets

    monkeypatch.setenv("SANDBOX_SECRETS_FILE", str(tmp_path / "does-not-exist"))
    _load_sandbox_secrets()


# ---------------------------------------------------------------------------
# Provision-time manifest injection
# ---------------------------------------------------------------------------


def _injectable_manifest(agent_id: str):
    from agent_platform.registry.models import AgentManifest, SourceInfo

    return AgentManifest(
        id=agent_id,
        team="agent_studio",
        name="Injected",
        summary="An injected dynamic agent.",
        source=SourceInfo(entrypoint="m:f"),
    )


def test_injected_manifest_lets_unknown_agent_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A dynamically-registered agent absent from the sandbox's on-disk registry
    boots when its manifest is injected via ``SANDBOX_AGENT_MANIFEST_FILE``."""
    import json

    from agent_platform.registry import get_registry
    from agent_sandbox_runtime.entrypoint import _build_app

    agent_id = "agent_team_studio.agent_studio.injected-xyz"
    manifest_file = tmp_path / "agent-manifest.json"
    manifest_file.write_text(json.dumps(_injectable_manifest(agent_id).model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setenv("SANDBOX_AGENT_ID", agent_id)
    monkeypatch.setenv("SANDBOX_AGENT_MANIFEST_FILE", str(manifest_file))
    try:
        _build_app()  # must NOT SystemExit
        assert get_registry().get(agent_id) is not None
    finally:
        get_registry().unregister(agent_id)
        get_registry.cache_clear()


def test_malformed_injected_manifest_falls_through_to_unknown_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed injected JSON is best-effort ignored; the unknown-agent gate stays
    authoritative and still exits ``EXIT_UNKNOWN_AGENT``."""
    from agent_sandbox_runtime.entrypoint import EXIT_UNKNOWN_AGENT, _build_app

    manifest_file = tmp_path / "agent-manifest.json"
    manifest_file.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_AGENT_ID", "agent_team_studio.agent_studio.bad-xyz")
    monkeypatch.setenv("SANDBOX_AGENT_MANIFEST_FILE", str(manifest_file))
    with pytest.raises(SystemExit) as exc_info:
        _build_app()
    assert exc_info.value.code == EXIT_UNKNOWN_AGENT


def test_maybe_register_injected_manifest_registers_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json

    from agent_sandbox_runtime.entrypoint import _maybe_register_injected_manifest

    manifest_file = tmp_path / "agent-manifest.json"
    manifest_file.write_text(
        json.dumps(_injectable_manifest("agent_team_studio.agent_studio.u-1").model_dump(mode="json")),
        encoding="utf-8",
    )
    monkeypatch.setenv("SANDBOX_AGENT_MANIFEST_FILE", str(manifest_file))
    registered: list = []
    fake_registry = type("R", (), {"register": lambda self, m: registered.append(m)})()
    _maybe_register_injected_manifest(fake_registry)
    assert [m.id for m in registered] == ["agent_team_studio.agent_studio.u-1"]


def test_maybe_register_injected_manifest_noop_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_sandbox_runtime.entrypoint import _maybe_register_injected_manifest

    monkeypatch.delenv("SANDBOX_AGENT_MANIFEST_FILE", raising=False)
    called: list = []
    fake_registry = type("R", (), {"register": lambda self, m: called.append(m)})()
    _maybe_register_injected_manifest(fake_registry)  # no-op, no raise
    assert called == []


def test_maybe_register_injected_manifest_noop_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_sandbox_runtime.entrypoint import _maybe_register_injected_manifest

    monkeypatch.setenv("SANDBOX_AGENT_MANIFEST_FILE", str(tmp_path / "nope.json"))
    called: list = []
    fake_registry = type("R", (), {"register": lambda self, m: called.append(m)})()
    _maybe_register_injected_manifest(fake_registry)  # no-op, no raise
    assert called == []
