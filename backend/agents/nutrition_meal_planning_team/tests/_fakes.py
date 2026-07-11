"""Shared test fakes for the nutrition_meal_planning_team suite.

``FakeResult``/``FakeOrchestrator`` stand in for orchestrator responses, and
``patch_orch`` routes ``pipeline.get_orchestrator`` to a fake. Imported by
``test_pipeline.py`` and ``test_temporal_activities.py`` (one definition, DRY).
"""

from __future__ import annotations


class FakeResult:
    """Stand-in for an orchestrator response with a ``model_dump``."""

    def __init__(self, data: dict) -> None:
        self._data = dict(data)

    def model_dump(self) -> dict:
        return dict(self._data)


class FakeOrchestrator:
    """Returns a canned result, or raises ``exc`` from every pipeline method."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self._exc = exc

    def _result(self, client_id: str) -> FakeResult:
        if self._exc is not None:
            raise self._exc
        return FakeResult({"client_id": client_id})

    def get_nutrition_plan(self, req) -> FakeResult:
        return self._result(req.client_id)

    def regenerate_nutrition_plan(self, client_id: str) -> FakeResult:
        return self._result(client_id)

    def get_meal_plan(self, req) -> FakeResult:
        return self._result(req.client_id)


def patch_orch(monkeypatch, *, exc: Exception | None = None) -> None:
    """Route ``pipeline.get_orchestrator`` to a ``FakeOrchestrator``."""
    from nutrition_meal_planning_team import pipeline

    monkeypatch.setattr(pipeline, "get_orchestrator", lambda: FakeOrchestrator(exc=exc))
