from __future__ import annotations

import json

import pytest
from studiogrid.runtime.errors import SchemaValidationError
from studiogrid.runtime.orchestrator import Orchestrator, RunContext
from studiogrid.runtime.storage.postgres_store import PostgresStore
from studiogrid.runtime.storage.s3_store import S3Store


class ArtifactRegistry:
    def run(self, agent_id: str, task_envelope: dict) -> dict:
        del agent_id, task_envelope
        return {
            "kind": "ARTIFACT",
            "payload": {
                "artifact_type": "design_tokens",
                "format": "json",
                "payload": {"tokens": ["primary", "secondary"]},
            },
        }


class ReviewRegistry:
    def run(self, agent_id: str, task_envelope: dict) -> dict:
        del agent_id, task_envelope
        return {
            "kind": "REVIEW",
            "payload": {
                "gate": "consistency",
                "passed": True,
                "required_fixes": [],
            },
        }


def _orchestrator(registry: object) -> Orchestrator:
    return Orchestrator(PostgresStore(), S3Store(), registry, {}, None, {})


def test_create_run_and_status_transitions() -> None:
    orch = _orchestrator(ArtifactRegistry())

    project_id = orch.create_project(name="Visual Audit", idempotency_key="project")
    run = orch.create_run(project_id=project_id, idempotency_key="run")

    assert run.phase == "INTAKE"
    assert orch.store.runs[run.run_id]["status"] == "RUNNING"

    orch.set_phase(run_id=run.run_id, phase="DESIGN", idempotency_key="phase")
    orch.set_waiting_for_human(
        run_id=run.run_id,
        decision_id="dec_123",
        reason="Need explicit contrast exception",
        expires_at=None,
        idempotency_key="wait",
    )
    orch.set_running(run_id=run.run_id, idempotency_key="resume")
    orch.set_done(run_id=run.run_id, idempotency_key="done")

    run_row = orch.store.runs[run.run_id]
    assert run_row["phase"] == "DONE"
    assert run_row["status"] == "DONE"
    assert "updated_at" in run_row


def test_decision_lifecycle_round_trip() -> None:
    orch = _orchestrator(ArtifactRegistry())
    decision_id = orch.create_decision(
        run_id="run_1",
        title="Choose CTA styling",
        context="Need AA contrast",
        options=[{"key": "A", "label": "Blue"}, {"key": "B", "label": "Black"}],
        idempotency_key="decision",
    )

    orch.resolve_decision(decision_id=decision_id, selected_option_key="B", idempotency_key="resolve")

    decision = orch.get_decision(decision_id=decision_id)
    assert decision["status"] == "CHOSEN"
    assert decision["selected_option_key"] == "B"


def test_dispatch_task_persists_artifact_ref() -> None:
    orch = _orchestrator(ArtifactRegistry())
    ctx = RunContext(project_id="proj", run_id="run_1", phase="DESIGN", contract_version=1)
    task = orch.build_phase_tasks(ctx=ctx)[0]

    refs = orch.dispatch_task_to_agent(ctx=ctx, task=task, idempotency_key="dispatch")

    assert len(refs) == 1
    ref = refs[0]
    assert ref.artifact_type == "design_tokens"
    assert ref.version == 1
    assert ref.uri.startswith("s3://studiogrid/run_1/design_tokens/")


def test_dispatch_task_rejects_non_artifact_envelope() -> None:
    orch = _orchestrator(ReviewRegistry())
    ctx = RunContext(project_id="proj", run_id="run_2", phase="DESIGN", contract_version=1)

    with pytest.raises(SchemaValidationError, match="must emit ARTIFACT"):
        orch.dispatch_task_to_agent(
            ctx=ctx,
            task={"owner_agent": "design_lead", "task_id": "task_1"},
            idempotency_key="bad-dispatch",
        )


def test_persist_artifact_uses_raw_bytes_when_present() -> None:
    orch = _orchestrator(ArtifactRegistry())
    ctx = RunContext(project_id="proj", run_id="run_3", phase="HANDOFF", contract_version=1)

    ref = orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "report", "format": "md", "payload": {"body": "ignored"}},
        raw_bytes=b"# external markdown",
        idempotency_key="persist-bytes",
    )

    assert orch.s3.objects[ref.uri] == b"# external markdown"


def test_assemble_handoff_kit_contains_latest_artifacts_for_run_only() -> None:
    orch = _orchestrator(ArtifactRegistry())
    ctx = RunContext(project_id="proj", run_id="run_4", phase="HANDOFF", contract_version=1)

    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "tokens", "format": "json", "payload": {"ok": True}},
        raw_bytes=None,
        idempotency_key="tokens",
    )
    orch.persist_artifact(
        ctx=RunContext(project_id="proj", run_id="run_other", phase="HANDOFF", contract_version=1),
        artifact_payload={"artifact_type": "tokens", "format": "json", "payload": {"ok": False}},
        raw_bytes=None,
        idempotency_key="tokens-other",
    )

    kit_ref = orch.assemble_handoff_kit(ctx=ctx, idempotency_key="kit")
    kit_payload = json.loads(orch.s3.objects[kit_ref.uri].decode("utf-8"))

    assert kit_payload["run_id"] == "run_4"
    assert len(kit_payload["latest_artifacts"]) == 1
    assert kit_payload["latest_artifacts"][0]["run_id"] == "run_4"
