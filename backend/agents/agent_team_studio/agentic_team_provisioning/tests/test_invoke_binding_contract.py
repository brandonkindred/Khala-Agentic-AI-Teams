"""Failing-first contract tests for manifest-first persona/state binding.

These encode the locked precedence contract in
``system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md``
for ``invoke_generated_agent``: the resolved ``AgentManifest`` supplies the
*default* for each persona field, and an *explicitly-present* request-body field
overrides that default for a single invoke only (never written back).

``invoke_generated_agent`` does not yet consult the manifest — it takes persona
entirely from the caller-supplied body — so the binding-dependent assertions
below are expected to be red until the runtime-binding implementation lands. They
are marked ``xfail(strict=False)`` so CI stays green now and reports XPASS (at
which point the marker is removed) once binding is implemented. The plain
(non-xfail) tests assert behavior that holds *both* today and after binding — the
explicit-override direction, the no-manifest fallback, and inert body tools — so
they are green regression guards regardless of whether binding has landed.

The manifest is made resolvable through a monkeypatched registry keyed by the
invoked agent id, so the tests do not depend on exactly how the implementation
threads the resolved manifest identity to the entrypoint — only that it resolves
persona from the registry at all. The LLM is faked (records the composed system
prompt / granted tools) so no network call is made.
"""

from __future__ import annotations

import pytest

from agent_platform.registry.models import (
    AgentManifest,
    AgentStateSpec,
    CognitionSpec,
    SourceInfo,
)
from agent_team_studio.agentic_team_provisioning.runtime import agent_builder

_BINDING_UNIMPLEMENTED = pytest.mark.xfail(
    reason=(
        "manifest-first persona/state binding per ADR-015 is not yet implemented "
        "in invoke_generated_agent (persona still taken from the request body)"
    ),
    strict=False,
)


class _FakeResult:
    """Mimics a strands AgentResult: text is obtained via ``str(result)``."""

    def __init__(self, text: str) -> None:
        self.message = {"role": "assistant", "content": [{"text": text}]}
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeStrandsAgent:
    """Records the system prompt + tools it was built with; echoes a fixed reply."""

    last_system_prompt: str | None = None
    last_tools: object = None

    def __init__(self, **kwargs) -> None:
        type(self).last_system_prompt = kwargs.get("system_prompt")
        type(self).last_tools = kwargs.get("tools")

    def __call__(self, message: str) -> _FakeResult:
        return _FakeResult("ok")


@pytest.fixture
def fake_strands(monkeypatch: pytest.MonkeyPatch):
    _FakeStrandsAgent.last_system_prompt = None
    _FakeStrandsAgent.last_tools = None
    monkeypatch.setattr(agent_builder, "StrandsAgent", _FakeStrandsAgent)
    return _FakeStrandsAgent


def _manifest(**kwargs) -> AgentManifest:
    """Build an ``AgentManifest`` with binding-relevant defaults.

    Preconditions: keyword overrides are valid ``AgentManifest`` fields.
    Postconditions: returns a validated manifest whose ``id`` is ``"demo.worker"``
        unless overridden.
    """
    base = dict(
        id="demo.worker",
        team="demo",
        name="Worker",
        summary="Handles demo work",
        tags=["research"],
        cognition=CognitionSpec(tools=[]),
        source=SourceInfo(entrypoint="demo.worker:run"),
    )
    base.update(kwargs)
    return AgentManifest.model_validate(base)


def _install_manifest(monkeypatch: pytest.MonkeyPatch, manifest: AgentManifest | None) -> None:
    """Wire a registry whose ``get()`` returns ``manifest`` for the invoked id.

    Passing ``manifest=None`` installs a registry that resolves nothing (the
    no-manifest fallback path).
    """

    class _Reg:
        def get(self, agent_id: str, *, conn=None):
            if manifest is not None and agent_id == manifest.id:
                return manifest
            return None

    monkeypatch.setattr("agent_platform.registry.get_registry", lambda: _Reg())


def _body(manifest_id: str, **overrides) -> dict:
    """Minimal valid invoke body addressed at ``manifest_id``."""
    body = {"agent_name": manifest_id, "agent_id": manifest_id, "message": "hi"}
    body.update(overrides)
    return body


# --- Persona field precedence: manifest default vs explicit body override ------


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_manifest_role_binds_when_body_omits_it(
    fake_strands, monkeypatch: pytest.MonkeyPatch
):
    """Body omits ``role`` → the manifest summary is the resolved role."""
    manifest = _manifest(summary="Handles demo work")
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id))

    assert "Role: Handles demo work" in fake_strands.last_system_prompt


@pytest.mark.asyncio
async def test_body_role_overrides_manifest_default(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """An explicit body ``role`` wins over the manifest default for this invoke.

    Plain (green now): an explicit request value is honored both today (the only
    source) and after binding (as the override), so this guards the override
    direction — a future "manifest always wins" regression would fail it."""
    manifest = _manifest(summary="Manifest role")
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id, role="Body role"))

    assert "Role: Body role" in fake_strands.last_system_prompt
    assert "Manifest role" not in fake_strands.last_system_prompt


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_manifest_skills_bind_from_tags(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """Body omits ``skills`` → non-plumbing manifest tags become the Skills line."""
    manifest = _manifest(tags=["research", "analysis"])
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id))

    assert "Skills: research, analysis" in fake_strands.last_system_prompt


@pytest.mark.asyncio
async def test_body_skills_override_manifest_tags(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """An explicit body ``skills`` list wins over the manifest tags.

    Plain (green now): guards the override direction — see
    ``test_body_role_overrides_manifest_default``."""
    manifest = _manifest(tags=["research"])
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id, skills=["negotiation"]))

    assert "Skills: negotiation" in fake_strands.last_system_prompt
    assert "research" not in fake_strands.last_system_prompt


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_explicit_empty_skills_clears_manifest_default(
    fake_strands, monkeypatch: pytest.MonkeyPatch
):
    """Presence test: an omitted key inherits the manifest default, but an
    explicitly-present empty list clears the field (request wins)."""
    manifest = _manifest(tags=["research"])
    _install_manifest(monkeypatch, manifest)

    # Omitted → manifest tags bind.
    await agent_builder.invoke_generated_agent(_body(manifest.id))
    assert "Skills: research" in fake_strands.last_system_prompt

    # Explicitly cleared → no Skills line, manifest default not injected.
    await agent_builder.invoke_generated_agent(_body(manifest.id, skills=[]))
    assert "Skills:" not in fake_strands.last_system_prompt


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_manifest_expertise_binds_from_team(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """Body omits ``expertise`` → the manifest team is the resolved expertise."""
    manifest = _manifest(team="demo")
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id))

    assert "Expertise: demo" in fake_strands.last_system_prompt


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_skills_strip_studio_plumbing_tag(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """Plumbing markers ({generated, agentic_team_provisioning, studio}) must not
    surface in the Skills line; authored skill tags still bind."""
    manifest = _manifest(tags=["seo", "studio", "generated", "agentic_team_provisioning"])
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id))

    assert "Skills: seo" in fake_strands.last_system_prompt
    assert "studio" not in fake_strands.last_system_prompt
    assert "generated" not in fake_strands.last_system_prompt


# --- system_prompt / state precedence -----------------------------------------


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_request_system_prompt_full_replacement(
    fake_strands, monkeypatch: pytest.MonkeyPatch
):
    """An explicit request ``system_prompt`` fully replaces the base prompt —
    persona fields are not spliced in a second time."""
    manifest = _manifest(summary="Manifest role")
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id, system_prompt="CUSTOM PROMPT"))

    assert "CUSTOM PROMPT" in fake_strands.last_system_prompt
    # Full replacement → the generic persona composer's intro is gone.
    assert "specialist agent" not in fake_strands.last_system_prompt


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_manifest_state_prompt_composed_with_persona(
    fake_strands, monkeypatch: pytest.MonkeyPatch
):
    """A manifest-sourced state prompt is composed with (not a replacement of) the
    resolved persona fields."""
    manifest = _manifest(
        summary="Manifest role",
        states=[
            AgentStateSpec(key="executing", label="Executing", system_prompt="STATE INSTRUCTION")
        ],
    )
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id))

    # Persona still binds ...
    assert "Role: Manifest role" in fake_strands.last_system_prompt
    # ... and the authored state prompt is appended.
    assert "STATE INSTRUCTION" in fake_strands.last_system_prompt


@_BINDING_UNIMPLEMENTED
@pytest.mark.asyncio
async def test_state_selects_matching_agentstatespec(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """The request ``state`` selects which ``AgentStateSpec`` backs the prompt."""
    manifest = _manifest(
        states=[
            AgentStateSpec(key="executing", label="Executing", system_prompt="EXEC"),
            AgentStateSpec(key="planning", label="Planning", system_prompt="PLAN"),
        ],
    )
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id, state="planning"))

    assert "PLAN" in fake_strands.last_system_prompt
    assert "EXEC" not in fake_strands.last_system_prompt


# --- Behavior guards (plain, green now) ---------------------------------------


@pytest.mark.asyncio
async def test_no_manifest_falls_back_to_body_values(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """No resolvable manifest → every field falls back to the request body,
    identical to today's behavior (a well-defined degraded path, not a failure)."""
    _install_manifest(monkeypatch, None)

    await agent_builder.invoke_generated_agent(
        _body("no.such.agent", role="Body role", skills=["body skill"])
    )

    assert "Role: Body role" in fake_strands.last_system_prompt
    assert "Skills: body skill" in fake_strands.last_system_prompt


@pytest.mark.asyncio
async def test_body_tools_never_granted(fake_strands, monkeypatch: pytest.MonkeyPatch):
    """The request ``tools`` field stays inert regardless of the manifest — the
    runtime grants no tools on this entrypoint (no python/http escalation)."""
    manifest = _manifest(cognition=CognitionSpec(tools=["python", "http_request"]))
    _install_manifest(monkeypatch, manifest)

    await agent_builder.invoke_generated_agent(_body(manifest.id, tools=["python", "http_request"]))

    assert fake_strands.last_tools == []
