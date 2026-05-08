"""Sanity check for the Temporal activity's call shape.

Before this was fixed, the activity called ``run_workflow(**request)`` where
``request`` was a dict, but the actual signature is
``run_workflow(run_id, store, agent)``. That meant the activity would
always crash the moment it executed — the workflow could never succeed.

The activity now takes a plain ``run_id`` string, reconstructs the store
and agent locally, and delegates to the orchestrator. These tests pin
that contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_activity_calls_orchestrator_with_reconstructed_deps(monkeypatch):
    """The activity must build store + agent inside, not expect them as kwargs."""
    import user_agent_founder.temporal as uaf_temporal

    fake_store = MagicMock(name="FounderRunStore")
    # No run row → activity skips persona lookup and uses the default agent.
    fake_store.get_run.return_value = None
    fake_persona_store = MagicMock(name="PersonaStore")
    fake_agent = MagicMock(name="FounderAgent")
    monkeypatch.setattr("user_agent_founder.store.get_founder_store", lambda: fake_store)
    monkeypatch.setattr("user_agent_founder.store.get_persona_store", lambda: fake_persona_store)
    # FounderAgent now takes optional prompt kwargs; the default-path call
    # passes none.
    monkeypatch.setattr("user_agent_founder.agent.FounderAgent", lambda *args, **kwargs: fake_agent)

    captured: dict = {}

    def _fake_run_workflow(run_id, store, agent):
        captured["run_id"] = run_id
        captured["store"] = store
        captured["agent"] = agent

    monkeypatch.setattr("user_agent_founder.orchestrator.run_workflow", _fake_run_workflow)

    result = uaf_temporal.run_pipeline_activity("run-xyz")

    assert result == {"run_id": "run-xyz"}
    assert captured == {"run_id": "run-xyz", "store": fake_store, "agent": fake_agent}
    # Default path: persona store is never queried when run row is None.
    fake_persona_store.get_persona.assert_not_called()


def test_activity_signature_is_string_not_dict():
    """Regression guard: passing a dict must not silently succeed."""
    import inspect

    import user_agent_founder.temporal as uaf_temporal

    sig = inspect.signature(uaf_temporal.run_pipeline_activity)
    params = list(sig.parameters.values())
    assert len(params) == 1
    # ``from __future__ import annotations`` stringifies annotations.
    assert params[0].annotation in (str, "str")
