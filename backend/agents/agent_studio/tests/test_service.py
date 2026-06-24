"""Unit tests for :mod:`agent_studio.service`.

The service is driven with an injected scripted assistant, an in-memory store,
and a fake registry so no live LLM or process-wide registry is touched.
"""

from __future__ import annotations

import pytest

from agent_registry.models import AgentManifest, CognitionSpec, IOSchema, SourceInfo
from agent_studio.assistant import AgentDesignerAgent
from agent_studio.models import AgentDefinition
from agent_studio.service import AgentStudioService
from agent_studio.store import AgentStudioConversationStore


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered: dict[str, AgentManifest] = {}
        self._seed: dict[str, AgentManifest] = {}

    def seed(self, manifest: AgentManifest) -> None:
        self._seed[manifest.id] = manifest

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._seed.get(agent_id) or self.registered.get(agent_id)

    def register(self, manifest: AgentManifest) -> None:
        self.registered[manifest.id] = manifest


def _seed_manifest() -> AgentManifest:
    return AgentManifest(
        id="blogging.planner",
        team="blogging",
        name="Planner",
        summary="Plans blog outlines",
        tags=["content"],
        cognition=CognitionSpec(rule_packs=["default_guardrails"], tools=["web.search"]),
        inputs=IOSchema(schema_ref="x:In"),
        outputs=IOSchema(schema_ref="x:Out"),
        source=SourceInfo(entrypoint="x:run"),
    )


_DRAFT_REPLY = """\
Drafted it.

```agent
{"name": "my.agent", "role": "Does a thing"}
```
"""


def _service(reply: str = _DRAFT_REPLY) -> tuple[AgentStudioService, _FakeRegistry]:
    registry = _FakeRegistry()
    assistant = AgentDesignerAgent(complete=lambda _s, _p: reply)
    svc = AgentStudioService(
        assistant=assistant,
        store=AgentStudioConversationStore(),
        registry_getter=lambda: registry,
    )
    return svc, registry


# ── start_conversation ───────────────────────────────────────────────────────


def test_start_new_returns_greeting_and_defaults() -> None:
    svc, _ = _service()
    state = svc.start_conversation("new", None, None)
    assert state.mode == "new"
    assert len(state.messages) == 1
    assert state.messages[0].role == "assistant"
    assert state.definition.mode == "new"
    assert state.readiness == ["name", "role"]
    assert state.suggested_questions  # default suggestions seeded


def test_start_new_with_initial_message_runs_a_turn() -> None:
    svc, _ = _service()
    state = svc.start_conversation("new", None, "Build a planner")
    # user + assistant turn instead of the canned greeting.
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert state.definition.name == "my.agent"
    assert state.readiness == []


def test_start_refine_clones_source() -> None:
    svc, registry = _service()
    registry.seed(_seed_manifest())
    state = svc.start_conversation("refine", "blogging.planner", None)
    assert state.mode == "refine"
    assert state.definition.mode == "refine"
    assert state.definition.cloned_from == "blogging.planner"
    assert state.definition.name == "Planner.copy"


def test_start_refine_without_source_raises_value_error() -> None:
    svc, _ = _service()
    with pytest.raises(ValueError):
        svc.start_conversation("refine", None, None)


def test_start_refine_unknown_source_raises_lookup_error() -> None:
    svc, _ = _service()
    with pytest.raises(LookupError):
        svc.start_conversation("refine", "missing", None)


# ── send_message ─────────────────────────────────────────────────────────────


def test_send_message_appends_turn_and_updates_definition() -> None:
    svc, _ = _service()
    started = svc.start_conversation("new", None, None)
    state = svc.send_message(started.conversation_id, "make a planner")
    # greeting + user + assistant
    assert [m.role for m in state.messages] == ["assistant", "user", "assistant"]
    assert state.definition.name == "my.agent"


def test_send_message_unknown_conversation_raises_lookup_error() -> None:
    svc, _ = _service()
    with pytest.raises(LookupError):
        svc.send_message("nope", "hi")


def test_send_message_without_block_leaves_definition() -> None:
    svc, _ = _service(reply="Tell me more first.")
    started = svc.start_conversation("new", None, None)
    state = svc.send_message(started.conversation_id, "vague")
    assert state.definition.name == ""
    assert state.suggested_questions == []


# ── clone_from_registry ──────────────────────────────────────────────────────


def test_clone_from_registry_returns_refine_draft() -> None:
    svc, registry = _service()
    registry.seed(_seed_manifest())
    draft = svc.clone_from_registry("blogging.planner")
    assert draft.mode == "refine"
    assert draft.cloned_from == "blogging.planner"


def test_clone_from_registry_unknown_raises_lookup_error() -> None:
    svc, _ = _service()
    with pytest.raises(LookupError):
        svc.clone_from_registry("missing")


# ── save_agent ───────────────────────────────────────────────────────────────


def test_save_agent_registers_manifest() -> None:
    svc, registry = _service()
    manifest = svc.save_agent(AgentDefinition(name="Saver", role="Saves things"))
    assert manifest.id in registry.registered
    assert registry.get(manifest.id) is manifest
    assert manifest.team == "agent_studio"


def test_save_agent_not_ready_raises_value_error() -> None:
    svc, registry = _service()
    with pytest.raises(ValueError) as exc:
        svc.save_agent(AgentDefinition(name="OnlyName"))
    assert "role" in str(exc.value)
    assert registry.registered == {}
