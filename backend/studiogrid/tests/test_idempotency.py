from studiogrid.runtime.orchestrator import Orchestrator, RunContext
from studiogrid.runtime.storage.postgres_store import PostgresStore
from studiogrid.runtime.storage.s3_store import S3Store


class DummyRegistry:
    def run(self, agent_id: str, task_envelope: dict) -> dict:
        del agent_id, task_envelope
        return {"kind": "ARTIFACT", "payload": {"artifact_type": "task_out", "format": "json", "payload": {"ok": True}}}


def test_idempotent_create_project_returns_same_id():
    orch = Orchestrator(PostgresStore(), S3Store(), DummyRegistry(), {}, None, {})
    id_one = orch.create_project(name="Demo", idempotency_key="k")
    id_two = orch.create_project(name="Demo", idempotency_key="k")
    assert id_one == id_two


def test_idempotent_persist_artifact_returns_same_ref():
    orch = Orchestrator(PostgresStore(), S3Store(), DummyRegistry(), {}, None, {})
    ctx = RunContext(project_id="p", run_id="r", phase="INTAKE", contract_version=1)
    payload = {"artifact_type": "intake", "format": "json", "payload": {"a": 1}}
    ref_one = orch.persist_artifact(ctx=ctx, artifact_payload=payload, raw_bytes=None, idempotency_key="k")
    ref_two = orch.persist_artifact(ctx=ctx, artifact_payload=payload, raw_bytes=None, idempotency_key="k")
    assert ref_one == ref_two
