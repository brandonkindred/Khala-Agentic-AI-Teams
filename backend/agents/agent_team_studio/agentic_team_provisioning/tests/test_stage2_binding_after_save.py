"""Stage-2 verification: does a *saved* manifest drive the Stage-2 run?

Where ``test_invoke_binding_contract.py`` unit-tests ``invoke_generated_agent``
against a monkeypatched registry, this suite verifies the **integration spine** the
epic promises to a user: build/save an agent in Stage 1, then run it in Stage 2 and
check the persisted persona binds. Every case starts from a *real* Stage-1 save —
``build_studio_agent_manifest`` / ``build_agent_manifest`` → ``AgentRegistry.register``
into a live in-process registry — and then drives one of the three Stage-2 invoke
paths ADR-015 scopes:

* **Pipeline runner** and **Studio test-chat** resolve persona from the manifest
  (``resolve_persona`` → ``build_agent``) *today*, so their binding is proven with
  plain, green assertions: real evidence the Stage-2 promise already holds there.
* **Sandbox invoke** (``POST /_agents/{id}/invoke`` → dispatch → the shared
  generated-agent entrypoint) still composes persona from the request body and never
  reads the resolved manifest. Its binding assertion is therefore ``xfail`` — it
  reports XFAIL on today's *unbound* runtime and will flip to XPASS once the
  runtime-binding implementation lands (at which point the marker is removed). That
  XFAIL↔XPASS transition is the "failures clearly indicate unbound vs bound
  behavior" signal this verification exists to give.

The green sandbox cases (explicit-body override honored; runtime tools stay inert)
guard the save → shim → dispatch → entrypoint wiring and hold both before and after
binding. The LLM is faked (records the composed system prompt / granted tools); no
network call is made and no Postgres is required (the registry is in-process).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.registry.loader import AgentRegistry
from agent_platform.studio.models import AgentDefinition
from agent_platform.studio.registration import build_studio_agent_manifest
from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.roster_resolve import resolve_persona
from agent_team_studio.agentic_team_provisioning.runtime import agent_builder
from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner
from shared.agent_invoke import mount_invoke_shim

# The sandbox invoke path composes persona from the request body and does not yet
# read the resolved manifest, so its binding-after-save assertion is red today. The
# reason names the unbound-vs-bound distinction; ``strict=False`` reports XFAIL now
# and XPASS (not a failure) once binding lands — that transition is the signal.
_SANDBOX_UNBOUND = pytest.mark.xfail(
    reason=(
        "sandbox invoke Stage-2 still composes persona from the request body, not the "
        "saved manifest — manifest-first binding per ADR-015 is not yet implemented in "
        "invoke_generated_agent (remove this marker when it XPASSes)"
    ),
    strict=False,
)


class _FakeResult:
    """Mimics a strands ``AgentResult``: text is obtained via ``str(result)``."""

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
    """Swap the real strands model for the recording double (no network call)."""
    _FakeStrandsAgent.last_system_prompt = None
    _FakeStrandsAgent.last_tools = None
    monkeypatch.setattr(agent_builder, "StrandsAgent", _FakeStrandsAgent)
    return _FakeStrandsAgent


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> AgentRegistry:
    """A live in-process registry, resolvable everywhere ``get_registry`` is read.

    Both the sandbox shim and ``resolve_persona`` do ``from agent_platform.registry
    import get_registry`` and call it at invoke time, so patching the single
    ``agent_platform.registry.get_registry`` attribute routes every Stage-2 path at
    this registry — the same wiring production uses, minus Postgres.
    """
    reg = AgentRegistry([], {})
    monkeypatch.setattr("agent_platform.registry.get_registry", lambda: reg)
    return reg


def _save_studio_agent(registry: AgentRegistry, definition: AgentDefinition):
    """Stage-1 save: build a Studio manifest and register it. Returns the manifest."""
    manifest = build_studio_agent_manifest(definition)
    registry.register(manifest)
    return manifest


def _save_generated_agent(registry: AgentRegistry, **kwargs):
    """Stage-1 save: build a generated-team manifest and register it."""
    manifest = build_agent_manifest("agentic_team_provisioning", **kwargs)
    registry.register(manifest)
    return manifest


def _shim_client() -> TestClient:
    """A TestClient over an app carrying only the sandbox invoke shim."""
    app = FastAPI()
    mount_invoke_shim(app)
    return TestClient(app)


# --- Stage-1 save wiring the Stage-2 invoke depends on ------------------------


def test_saved_studio_agent_is_resolvable_with_shared_entrypoint(registry: AgentRegistry):
    """A Stage-1 save registers a manifest that Stage-2 can resolve and dispatch.

    The saved agent must resolve by id and carry the shared generated-agent
    entrypoint — the contract every Stage-2 path (sandbox/pipeline/test-chat) relies
    on to run a saved agent without a YAML file.
    """
    manifest = _save_studio_agent(
        registry, AgentDefinition(name="Contract Auditor", role="Audits vendor contracts")
    )

    resolved = registry.get(manifest.id)
    assert resolved is not None
    assert "agentic_team_provisioning" in resolved.source.entrypoint
    assert resolved.source.entrypoint.endswith(":invoke_generated_agent")


# --- Pipeline runner Stage-2 (bound today → green) ---------------------------


def test_pipeline_stage2_binds_saved_persona_after_save(fake_strands, registry: AgentRegistry):
    """The pipeline runner composes its prompt from the saved manifest's persona.

    Real evidence the Stage-2 promise already holds on this path: ``_run_agent``
    resolves ``role`` / ``skills`` / ``expertise`` from the registered manifest via
    ``resolve_persona`` — the caller never supplies them.
    """
    manifest = _save_generated_agent(
        registry,
        agent_name="Market Researcher",
        summary="Researches markets",
        skill_tags=["research", "analysis"],
    )
    thin = AgenticTeamAgent.model_construct(
        agent_name="Market Researcher", source="generated", manifest_id=manifest.id
    )

    output = PipelineRunner._run_agent(thin, "Analyze the EV market")

    assert output == "ok"
    prompt = fake_strands.last_system_prompt
    assert "Role: Researches markets" in prompt
    assert "Skills: research, analysis" in prompt
    assert "Expertise: agentic_team_provisioning" in prompt


# --- Studio test-chat Stage-2 (bound today → green) --------------------------


def test_test_chat_stage2_binds_saved_persona_after_save(fake_strands, registry: AgentRegistry):
    """The Studio test-chat path binds the saved persona.

    ``send_test_chat_message`` resolves persona from the manifest and builds the
    agent through ``resolve_persona`` → ``build_agent`` (``_build_test_agent`` is an
    alias of ``build_agent``); this exercises those exact production calls.
    """
    manifest = _save_studio_agent(
        registry,
        AgentDefinition(
            name="Support Concierge",
            role="Handles customer support",
            tags=["support", "billing"],
        ),
    )

    persona = resolve_persona(manifest.id)
    agent_instance = agent_builder.build_agent(
        "Support Concierge",
        persona.role,
        persona.skills,
        persona.capabilities,
        persona.tools,
        persona.expertise,
    )
    response = agent_builder.call_agent(agent_instance, "How do I get a refund?")

    assert response == "ok"
    prompt = fake_strands.last_system_prompt
    assert "Role: Handles customer support" in prompt
    assert "support" in prompt and "billing" in prompt


# --- Sandbox invoke Stage-2 (unbound today → xfail until binding lands) -------


@_SANDBOX_UNBOUND
def test_sandbox_invoke_stage2_binds_saved_persona_after_save(
    fake_strands, registry: AgentRegistry
):
    """Invoking a saved agent without re-supplying persona must bind the manifest.

    This is the user-visible Stage-2 promise: after Stage-1 save, a sandbox invoke
    with the persona fields omitted from the body should be driven by the saved
    ``role`` and the authored ``executing``-state ``system_prompt`` — resolved via
    the URL ``agent_id`` exactly as ADR-015 mandates. It is ``xfail`` until
    manifest-first binding lands in ``invoke_generated_agent``.
    """
    manifest = _save_studio_agent(
        registry,
        AgentDefinition(
            name="Contract Auditor",
            role="Audits vendor contracts",
            tags=["legal", "contracts"],
            system_prompt="Always cite the clause number.",
        ),
    )

    response = _shim_client().post(
        f"/_agents/{manifest.id}/invoke",
        json={"agent_name": manifest.name, "message": "Review this MSA."},
    )
    assert response.status_code == 200

    prompt = fake_strands.last_system_prompt
    # Bound persona: the saved role and the authored executing-state prompt drive
    # the run even though the request body carried neither.
    assert "Role: Audits vendor contracts" in prompt
    assert "Always cite the clause number." in prompt


def test_sandbox_invoke_stage2_honors_explicit_body_override(fake_strands, registry: AgentRegistry):
    """An explicit body persona is honored through the real save → shim → dispatch
    → entrypoint wiring.

    Green both before and after binding: today the body is the only persona source,
    and after binding an explicitly-present request field wins over the manifest
    default (ADR-015). This guards the end-to-end Stage-2 wiring and the override
    direction against a future "manifest always wins" regression.
    """
    manifest = _save_studio_agent(
        registry, AgentDefinition(name="Contract Auditor", role="Audits vendor contracts")
    )

    response = _shim_client().post(
        f"/_agents/{manifest.id}/invoke",
        json={
            "agent_name": "Contract Auditor",
            "message": "Review this MSA.",
            "role": "Explicit Body Role",
            "skills": ["negotiation"],
        },
    )
    assert response.status_code == 200

    prompt = fake_strands.last_system_prompt
    assert "Role: Explicit Body Role" in prompt
    assert "Skills: negotiation" in prompt


def test_sandbox_invoke_stage2_never_grants_tools(fake_strands, registry: AgentRegistry):
    """The sandbox entrypoint grants no runtime tools, even for a saved manifest that
    advertises them.

    Green both before and after binding: ADR-015 keeps the request ``tools`` field
    inert and does not feed ``manifest.cognition.tools`` into the runtime — a caller
    (or a saved manifest naming ``python`` / ``http_request``) cannot escalate to an
    unaudited code/network capability on this path.
    """
    manifest = _save_studio_agent(
        registry,
        AgentDefinition(
            name="Toolful Agent",
            role="Advertises tools it should not be granted",
            tools=["python", "http_request"],
        ),
    )
    assert manifest.cognition.tools == ["python", "http_request"]

    response = _shim_client().post(
        f"/_agents/{manifest.id}/invoke",
        json={"agent_name": "Toolful Agent", "message": "run code", "tools": ["python"]},
    )
    assert response.status_code == 200
    assert fake_strands.last_tools == []
