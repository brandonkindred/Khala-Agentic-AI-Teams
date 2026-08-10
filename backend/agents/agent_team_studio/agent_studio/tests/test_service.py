"""Unit tests for :mod:`agent_team_studio.agent_studio.service`.

The service is driven with an injected scripted assistant, an in-memory store,
and a fake registry so no live LLM or process-wide registry is touched.
"""

from __future__ import annotations

import pytest

from agent_registry.models import AgentManifest
from agent_team_studio.agent_studio.assistant import AgentDesignerAgent
from agent_team_studio.agent_studio.models import AgentDefinition
from agent_team_studio.agent_studio.service import AgentStudioService
from agent_team_studio.agent_studio.store import AgentStudioConversationStore
from agent_team_studio.agent_studio.testing import FakeRegistry, seed_manifest

_DRAFT_REPLY = """\
Drafted it.

```agent
{"name": "my.agent", "role": "Does a thing"}
```
"""


def _service(reply: str = _DRAFT_REPLY) -> tuple[AgentStudioService, FakeRegistry]:
    registry = FakeRegistry()
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
    registry.seed(seed_manifest())
    state = svc.start_conversation("refine", "blogging.planner", None)
    assert state.mode == "refine"
    assert state.definition.mode == "refine"
    assert state.definition.cloned_from == "blogging.planner"
    assert state.definition.name == "Planner.copy"


def test_start_refine_without_source_raises_value_error() -> None:
    svc, _ = _service()
    with pytest.raises(ValueError):
        svc.start_conversation("refine", None, None)


def test_start_new_with_source_raises_value_error() -> None:
    # A new build has no source; passing one is a client error, not ignored.
    svc, _ = _service()
    with pytest.raises(ValueError):
        svc.start_conversation("new", "some.agent", None)


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


def test_send_message_assistant_failure_leaves_no_partial_message() -> None:
    # If the assistant raises, the user message must NOT be persisted (it's
    # appended only after a successful respond), so the conversation isn't left
    # with a dangling user turn and no reply.
    class _BoomAssistant:
        def respond(self, *_args):
            raise RuntimeError("llm down")

    store = AgentStudioConversationStore()
    svc = AgentStudioService(
        assistant=_BoomAssistant(), store=store, registry_getter=lambda: FakeRegistry()
    )
    started = svc.start_conversation("new", None, None)  # greeting only
    cid = started.conversation_id
    before = len(store.get(cid).messages)
    with pytest.raises(RuntimeError):
        svc.send_message(cid, "hi")
    assert len(store.get(cid).messages) == before


def test_start_conversation_initial_message_failure_discards_conversation() -> None:
    # If the very first turn fails, start_conversation must roll back the
    # just-created conversation rather than leaving an orphaned empty record.
    class _BoomAssistant:
        def respond(self, *_args):
            raise RuntimeError("llm down")

    store = AgentStudioConversationStore()
    svc = AgentStudioService(
        assistant=_BoomAssistant(), store=store, registry_getter=lambda: FakeRegistry()
    )
    with pytest.raises(RuntimeError):
        svc.start_conversation("new", None, "build me a planner")
    assert len(store) == 0  # no orphaned conversation left behind


def test_fake_registry_register_takes_precedence_over_seed() -> None:
    # Regression for the test double: a registered agent must not be shadowed by
    # a seed of the same id.
    registry = FakeRegistry()
    seeded = seed_manifest(agent_id="dup.id", summary="seeded")
    registry.seed(seeded)
    replacement = seed_manifest(agent_id="dup.id", summary="registered")
    registry.register(replacement)
    assert registry.get("dup.id").summary == "registered"


# ── clone_from_registry ──────────────────────────────────────────────────────


def test_clone_from_registry_returns_refine_draft() -> None:
    svc, registry = _service()
    registry.seed(seed_manifest())
    draft = svc.clone_from_registry("blogging.planner")
    assert draft.mode == "refine"
    assert draft.cloned_from == "blogging.planner"


def test_clone_from_registry_unknown_raises_lookup_error() -> None:
    svc, _ = _service()
    with pytest.raises(LookupError):
        svc.clone_from_registry("missing")


def test_clone_from_registry_persists_no_second_identity() -> None:
    # Regression for #5896: clone-from-registry only reads the source manifest
    # (registry.get) — it must never register anything, so no second persisted
    # identity is created as a side effect of cloning.
    svc, registry = _service()
    registry.seed(seed_manifest())
    draft = svc.clone_from_registry("blogging.planner")
    assert isinstance(draft, AgentDefinition)
    assert not isinstance(draft, AgentManifest)
    assert registry.registered == {}


# ── save_agent ───────────────────────────────────────────────────────────────


def test_save_agent_registers_manifest() -> None:
    svc, registry = _service()
    manifest, created = svc.save_agent(AgentDefinition(name="Saver", role="Saves things"))
    assert manifest.id in registry.registered
    assert registry.get(manifest.id) is manifest
    assert manifest.team == "agent_studio"
    assert created is True


def test_save_agent_same_name_updates_in_place_and_reports_not_created() -> None:
    # Name is identity: a second save with the same name replaces the first
    # (one entry), and `created` is False so the overwrite isn't silent.
    svc, registry = _service()
    first, created_first = svc.save_agent(AgentDefinition(name="Dup", role="v1"))
    second, created_second = svc.save_agent(AgentDefinition(name="Dup", role="v2"))
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(registry.registered) == 1
    assert registry.get(second.id).summary == "v2"


def test_save_agent_not_ready_raises_value_error() -> None:
    svc, registry = _service()
    with pytest.raises(ValueError) as exc:
        svc.save_agent(AgentDefinition(name="OnlyName"))
    assert "role" in str(exc.value)
    assert registry.registered == {}


def test_save_agent_persists_only_agent_manifest_type() -> None:
    # Regression for #5895: AgentManifest is the sole persisted catalog identity
    # — save_agent must never register an AgentDefinition (or any other second
    # identity type) alongside or instead of the manifest.
    svc, registry = _service()
    manifest, _created = svc.save_agent(AgentDefinition(name="Saver", role="Saves things"))
    stored = registry.registered[manifest.id]
    assert isinstance(stored, AgentManifest)
    assert not isinstance(stored, AgentDefinition)
    assert all(isinstance(v, AgentManifest) for v in registry.registered.values())
