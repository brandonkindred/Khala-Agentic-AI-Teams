"""CodeEngineProvider registry + provider-required worker construction."""

from __future__ import annotations

import pytest

from coding_team import orchestrator as orch_mod
from coding_team.engine_provider import get_engine_provider, set_engine_provider
from coding_team.models import StackSpec


def test_registry_set_get_and_clear(monkeypatch) -> None:
    # Isolate the process-wide global; monkeypatch reverts it after the test.
    monkeypatch.setattr("coding_team.engine_provider._provider", None)
    assert get_engine_provider() is None

    sentinel = object()
    set_engine_provider(sentinel)
    assert get_engine_provider() is sentinel

    set_engine_provider(None)
    assert get_engine_provider() is None


def test_build_worker_requires_a_provider() -> None:
    """A supported stack with no injected provider is a configuration error, not a crash-later."""
    with pytest.raises(RuntimeError, match="No CodeEngineProvider"):
        orch_mod._build_implementation_worker(
            "backend_v2",
            StackSpec(name="backend_v2", tools_services=["Python"]),
            lambda _key: None,
            None,
        )
