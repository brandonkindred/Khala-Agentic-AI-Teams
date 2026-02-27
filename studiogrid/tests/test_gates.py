from studiogrid.runtime.orchestrator import Orchestrator, RunContext
from studiogrid.runtime.storage.postgres_store import PostgresStore
from studiogrid.runtime.storage.s3_store import S3Store


class DummyRegistry:
    def run(self, agent_id: str, task_envelope: dict) -> dict:
        del agent_id, task_envelope
        return {"kind": "ARTIFACT", "payload": {"artifact_type": "mock", "format": "json", "payload": {}}}


def test_gates_report_failures():
    orch = Orchestrator(PostgresStore(), S3Store(), DummyRegistry(), {}, None, {})
    ctx = RunContext(project_id="p1", run_id="r1", phase="DISCOVERY", contract_version=1)
    results = orch.run_gates_for_phase(ctx=ctx, gates=["force_fail"], idempotency_key="k1")
    assert results[0].passed is False
