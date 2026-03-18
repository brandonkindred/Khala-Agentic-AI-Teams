from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from studiogrid.runtime.errors import SchemaValidationError
from studiogrid.runtime.validators.schema_validator import validate_envelope, validate_payload


@dataclass(frozen=True)
class RunContext:
    project_id: str
    run_id: str
    phase: str
    contract_version: int


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str
    version: int
    format: str
    uri: str


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    review_ids: list[str]


class Orchestrator:
    def __init__(self, store, s3, registry, validators, router, policies):
        self.store = store
        self.s3 = s3
        self.registry = registry
        self.validators = validators
        self.router = router
        self.policies = policies

    def _idempotent(self, key: str, fn):
        existing = self.store.get_idempotency(key)
        if existing is not None:
            return existing
        value = fn()
        self.store.put_idempotency(key, value)
        return value

    def create_project(self, *, name: str, idempotency_key: str) -> str:
        return self._idempotent(
            idempotency_key,
            lambda: self._create_project(name),
        )

    def _create_project(self, name: str) -> str:
        project_id = f"proj_{uuid.uuid4().hex[:10]}"
        self.store.projects[project_id] = {"project_id": project_id, "name": name}
        return project_id

    def create_run(self, *, project_id: str, idempotency_key: str) -> RunContext:
        result = self._idempotent(idempotency_key, lambda: self._create_run(project_id))
        return RunContext(
            project_id=result["project_id"],
            run_id=result["run_id"],
            phase=result["phase"],
            contract_version=result["contract_version"],
        )

    def _create_run(self, project_id: str) -> dict:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        row = {
            "project_id": project_id,
            "run_id": run_id,
            "phase": "INTAKE",
            "status": "RUNNING",
            "contract_version": 1,
        }
        self.store.runs[run_id] = row
        return row

    def set_phase(self, *, run_id: str, phase: str, idempotency_key: str) -> None:
        self._idempotent(idempotency_key, lambda: self._set_run(run_id, phase=phase))

    def _set_run(self, run_id: str, **changes: Any) -> None:
        self.store.runs[run_id].update(changes)
        self.store.runs[run_id]["updated_at"] = datetime.utcnow().isoformat()

    def set_waiting_for_human(self, *, run_id: str, decision_id: str, reason: str, expires_at: str | None, idempotency_key: str) -> None:
        del reason, expires_at
        self._idempotent(
            idempotency_key,
            lambda: self._set_run(run_id, status="WAITING_FOR_HUMAN", waiting_decision_id=decision_id),
        )

    def set_running(self, *, run_id: str, idempotency_key: str) -> None:
        self._idempotent(idempotency_key, lambda: self._set_run(run_id, status="RUNNING"))

    def set_done(self, *, run_id: str, idempotency_key: str) -> None:
        self._idempotent(idempotency_key, lambda: self._set_run(run_id, status="DONE", phase="DONE"))

    def create_decision(self, *, run_id: str, title: str, context: str, options: list[dict[str, str]], idempotency_key: str) -> str:
        return self._idempotent(idempotency_key, lambda: self._create_decision(run_id, title, context, options))

    def _create_decision(self, run_id: str, title: str, context: str, options: list[dict[str, str]]) -> str:
        decision_id = f"dec_{uuid.uuid4().hex[:10]}"
        self.store.decisions[decision_id] = {
            "decision_id": decision_id,
            "run_id": run_id,
            "title": title,
            "context": context,
            "options": options,
            "status": "OPEN",
        }
        return decision_id

    def resolve_decision(self, *, decision_id: str, selected_option_key: str, idempotency_key: str) -> None:
        self._idempotent(idempotency_key, lambda: self._resolve_decision(decision_id, selected_option_key))

    def _resolve_decision(self, decision_id: str, selected_option_key: str) -> None:
        self.store.decisions[decision_id]["status"] = "CHOSEN"
        self.store.decisions[decision_id]["selected_option_key"] = selected_option_key

    def get_decision(self, *, decision_id: str) -> dict[str, Any]:
        return self.store.decisions[decision_id]

    def build_phase_tasks(self, *, ctx: RunContext) -> list[dict[str, Any]]:
        return [
            {
                "task_id": f"task_{ctx.phase.lower()}_{ctx.run_id}",
                "owner_agent": "startup_advisor_orchestrator",
                "inputs": [{"artifact_type": "intake"}],
                "outputs_expected": ["artifact"],
                "acceptance_criteria": ["valid envelope", "phase aligned"],
            }
        ]

    def dispatch_task_to_agent(self, *, ctx: RunContext, task: dict[str, Any], idempotency_key: str) -> list[ArtifactRef]:
        def _dispatch() -> list[dict]:
            envelope = self.registry.run(agent_id=task["owner_agent"], task_envelope={"ctx": asdict(ctx), "task": task})
            validate_envelope(envelope)
            kind = envelope["kind"]
            payload = envelope["payload"]
            validate_payload(kind, payload)
            if kind != "ARTIFACT":
                raise SchemaValidationError("Task agent must emit ARTIFACT envelope")
            ref = self.persist_artifact(ctx=ctx, artifact_payload=payload, raw_bytes=None, idempotency_key=f"{idempotency_key}:persist")
            return [asdict(ref)]

        refs = self._idempotent(idempotency_key, _dispatch)
        return [ArtifactRef(**ref) for ref in refs]

    def run_gates_for_phase(self, *, ctx: RunContext, gates: list[str], idempotency_key: str) -> list[GateResult]:
        del ctx
        def _run() -> list[dict]:
            results = []
            for gate in gates:
                review_id = f"rev_{uuid.uuid4().hex[:8]}"
                passed = gate != "force_fail"
                self.store.artifacts[review_id] = {"artifact_id": review_id, "artifact_type": "review", "payload": {"gate": gate, "passed": passed}}
                results.append({"gate": gate, "passed": passed, "review_ids": [review_id]})
            return results

        return [GateResult(**r) for r in self._idempotent(idempotency_key, _run)]

    def create_revision_tasks_from_reviews(self, *, ctx: RunContext, review_ids: list[str], idempotency_key: str) -> list[str]:
        del ctx
        return self._idempotent(idempotency_key, lambda: [f"revision_{rid}" for rid in review_ids])

    def persist_artifact(self, *, ctx: RunContext, artifact_payload: dict[str, Any], raw_bytes: bytes | None, idempotency_key: str) -> ArtifactRef:
        def _persist() -> dict:
            artifact_type = artifact_payload["artifact_type"]
            format_name = artifact_payload["format"]
            payload_obj = artifact_payload["payload"]
            version = self.store.next_artifact_version(ctx.run_id, artifact_type)
            artifact_id = f"art_{uuid.uuid4().hex[:10]}"
            key = f"{ctx.run_id}/{artifact_type}/v{version}.{format_name}"
            body = raw_bytes if raw_bytes is not None else json.dumps(payload_obj).encode("utf-8")
            uri = self.s3.put_bytes(key, body).uri
            record = {
                "artifact_id": artifact_id,
                "run_id": ctx.run_id,
                "artifact_type": artifact_type,
                "version": version,
                "format": format_name,
                "uri": uri,
                "payload": payload_obj,
            }
            self.store.artifacts[artifact_id] = record
            self.store.artifact_latest[(ctx.run_id, artifact_type)] = artifact_id
            return record

        row = self._idempotent(idempotency_key, _persist)
        return ArtifactRef(
            artifact_id=row["artifact_id"],
            artifact_type=row["artifact_type"],
            version=row["version"],
            format=row["format"],
            uri=row["uri"],
        )

    def assemble_handoff_kit(self, *, ctx: RunContext, idempotency_key: str) -> ArtifactRef:
        payload = {
            "artifact_type": "handoff_kit",
            "format": "json",
            "payload": {
                "run_id": ctx.run_id,
                "latest_artifacts": [
                    {"run_id": run_id, "artifact_type": atype, "artifact_id": aid}
                    for (run_id, atype), aid in self.store.artifact_latest.items()
                    if run_id == ctx.run_id
                ],
            },
        }
        return self.persist_artifact(ctx=ctx, artifact_payload=payload, raw_bytes=None, idempotency_key=idempotency_key)
