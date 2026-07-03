"""Smoke test for the standalone composition root (coding_team_service.main).

The container's TEAM_MODULE points at coding_team_service.main; if a rename in
SECodeEngineProvider, coding_team.engine_provider, or coding_team.api.main
breaks it, the service crash-loops at deploy. Importing it here keeps that
wiring exercised (and coverage-measured) in CI.
"""

from __future__ import annotations

import importlib


def test_composition_root_installs_provider_and_exposes_app(monkeypatch) -> None:
    import coding_team.engine_provider as registry

    # Importing the composition root mutates the process-global provider
    # registry by design; snapshot/restore so this test cannot poison others.
    monkeypatch.setattr(registry, "_provider", registry._provider)

    svc = importlib.import_module("coding_team_service.main")
    # Re-execute the wiring even if another test already imported the module.
    importlib.reload(svc)

    from software_engineering_team.coding_engine_provider import SECodeEngineProvider

    assert isinstance(registry.get_engine_provider(), SECodeEngineProvider)

    from coding_team.api.main import app as coding_app

    assert svc.app is coding_app  # the container serves coding_team's app
