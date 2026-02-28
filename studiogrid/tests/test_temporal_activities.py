import importlib
import sys
import types

import pytest


def _load_activities_module():
    if "temporalio" not in sys.modules:
        temporalio = types.ModuleType("temporalio")

        class _ActivityShim:
            def defn(self, name=None):
                del name

                def decorator(fn):
                    return fn

                return decorator

        temporalio.activity = _ActivityShim()
        sys.modules["temporalio"] = temporalio

    return importlib.import_module("studiogrid.runtime.temporal_activities")


class _FakeGateResult:
    def __init__(self, passed: bool, review_ids: list[str]):
        self.passed = passed
        self.review_ids = review_ids


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.gate_idempotency_key = None
        self.revision_idempotency_key = None

    def build_phase_tasks(self, *, ctx):
        del ctx
        return []

    def run_gates_for_phase(self, *, ctx, gates, idempotency_key):
        del ctx, gates
        self.gate_idempotency_key = idempotency_key
        return [_FakeGateResult(False, ["rev-1"])]

    def create_revision_tasks_from_reviews(self, *, ctx, review_ids, idempotency_key):
        del ctx, review_ids
        self.revision_idempotency_key = idempotency_key
        return ["task-1"]


@pytest.mark.anyio
async def test_run_revision_loop_uses_decision_scoped_idempotency(monkeypatch):
    acts = _load_activities_module()
    fake = _FakeOrchestrator()
    monkeypatch.setattr(acts, "build_orchestrator", lambda: fake)

    payload = {
        "ctx": {"project_id": "p1", "run_id": "r1", "contract_version": 1},
        "phase": "DISCOVERY",
        "decision_id": "dec-77",
    }
    await acts.run_revision_loop(payload)

    assert fake.gate_idempotency_key == "r1:RunGates:DISCOVERY:revision:dec-77"
    assert fake.revision_idempotency_key == "r1:CreateRevisionTasks:DISCOVERY:revision:dec-77"
