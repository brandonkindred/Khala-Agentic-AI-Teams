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

    def set_waiting_for_human(
        self, *, run_id: str, decision_id: str, reason: str, expires_at: str | None, idempotency_key: str
    ) -> None:
        del reason, expires_at
        self._idempotent(
            idempotency_key,
            lambda: self._set_run(run_id, status="WAITING_FOR_HUMAN", waiting_decision_id=decision_id),
        )

    def set_running(self, *, run_id: str, idempotency_key: str) -> None:
        self._idempotent(idempotency_key, lambda: self._set_run(run_id, status="RUNNING"))

    def set_done(self, *, run_id: str, idempotency_key: str) -> None:
        self._idempotent(idempotency_key, lambda: self._set_run(run_id, status="DONE", phase="DONE"))

    def create_decision(
        self, *, run_id: str, title: str, context: str, options: list[dict[str, str]], idempotency_key: str
    ) -> str:
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

    # --- Specialist routing ---------------------------------------------------

    _DOMAIN_AGENT_MAP: dict[str, str] = {
        "customer_discovery": "customer_discovery_advisor",
        "product": "product_strategy_advisor",
        "growth": "growth_gtm_advisor",
        "gtm": "growth_gtm_advisor",
        "fundraising": "fundraising_finance_advisor",
        "finance": "fundraising_finance_advisor",
        "operations": "operations_legal_advisor",
        "legal": "operations_legal_advisor",
        "coaching": "founder_coach_advisor",
        "leadership": "founder_coach_advisor",
    }

    def _resolve_specialist(self, intake_payload: dict[str, Any]) -> str:
        """Pick the most appropriate specialist agent from the intake payload.

        Checks ``domain`` field first, then falls back to keyword scanning of
        ``brief``, ``description``, and ``goals`` fields.  Returns
        ``startup_advisor_orchestrator`` when no clear match is found.
        """
        domain = str(intake_payload.get("domain", "")).lower().strip()
        if domain in self._DOMAIN_AGENT_MAP:
            return self._DOMAIN_AGENT_MAP[domain]

        # Keyword scan across common free-text fields
        text = " ".join(
            str(intake_payload.get(field, "")) for field in ("brief", "description", "goals", "challenge", "question")
        ).lower()
        for keyword, agent_id in self._DOMAIN_AGENT_MAP.items():
            if keyword in text:
                return agent_id

        return "startup_advisor_orchestrator"

    # --- Task building -------------------------------------------------------

    def build_phase_tasks(self, *, ctx: RunContext) -> list[dict[str, Any]]:
        # Retrieve the intake artifact payload to determine which specialist to call
        intake_artifact_id = self.store.artifact_latest.get((ctx.run_id, "intake"))
        intake_payload: dict[str, Any] = {}
        if intake_artifact_id and intake_artifact_id in self.store.artifacts:
            intake_payload = self.store.artifacts[intake_artifact_id].get("payload", {})

        owner_agent = self._resolve_specialist(intake_payload)

        # Collect any prior artifacts produced in this run to give specialists context
        prior_artifacts = [
            {"artifact_id": aid, "artifact_type": atype}
            for (run_id, atype), aid in self.store.artifact_latest.items()
            if run_id == ctx.run_id and atype != "intake"
        ]

        return [
            {
                "task_id": f"task_{ctx.phase.lower()}_{ctx.run_id}",
                "owner_agent": owner_agent,
                "inputs": [{"artifact_type": "intake"}],
                "prior_artifacts": prior_artifacts,
                "outputs_expected": ["artifact"],
                "acceptance_criteria": ["valid envelope", "phase aligned"],
            }
        ]

    def dispatch_task_to_agent(
        self, *, ctx: RunContext, task: dict[str, Any], idempotency_key: str
    ) -> list[ArtifactRef]:
        def _dispatch() -> list[dict]:
            # Include prior artifact payloads in the task envelope for cross-specialist context
            prior_artifact_payloads = []
            for ref in task.get("prior_artifacts", []):
                aid = ref.get("artifact_id")
                if aid and aid in self.store.artifacts:
                    prior_artifact_payloads.append(
                        {
                            "artifact_id": aid,
                            "artifact_type": ref.get("artifact_type"),
                            "payload": self.store.artifacts[aid].get("payload"),
                        }
                    )

            task_envelope = {
                "ctx": asdict(ctx),
                "task": task,
                "prior_artifacts": prior_artifact_payloads,
            }
            envelope = self.registry.run(agent_id=task["owner_agent"], task_envelope=task_envelope)
            validate_envelope(envelope)
            kind = envelope["kind"]
            payload = envelope["payload"]
            validate_payload(kind, payload)
            if kind != "ARTIFACT":
                raise SchemaValidationError("Task agent must emit ARTIFACT envelope")
            ref = self.persist_artifact(
                ctx=ctx, artifact_payload=payload, raw_bytes=None, idempotency_key=f"{idempotency_key}:persist"
            )
            return [asdict(ref)]

        refs = self._idempotent(idempotency_key, _dispatch)
        return [ArtifactRef(**ref) for ref in refs]

    def run_gates_for_phase(self, *, ctx: RunContext, gates: list[str], idempotency_key: str) -> list[GateResult]:
        def _run() -> list[dict]:
            results = []
            for gate in gates:
                review_id = f"rev_{uuid.uuid4().hex[:8]}"
                passed, failure_reason = self._evaluate_gate(ctx=ctx, gate=gate)
                review_payload = {"gate": gate, "passed": passed}
                if failure_reason:
                    review_payload["failure_reason"] = failure_reason
                self.store.artifacts[review_id] = {
                    "artifact_id": review_id,
                    "artifact_type": "review",
                    "payload": review_payload,
                }
                results.append({"gate": gate, "passed": passed, "review_ids": [review_id]})
            return results

        return [GateResult(**r) for r in self._idempotent(idempotency_key, _run)]

    def _evaluate_gate(self, *, ctx: RunContext, gate: str) -> tuple[bool, str]:
        """Evaluate a named gate against the run's latest artifacts.

        Returns (passed, failure_reason).  ``failure_reason`` is empty when passed.
        """
        if gate == "force_fail":
            return False, "gate forced to fail"

        if gate == "schema":
            # Verify every non-intake artifact in this run has required envelope fields
            for (run_id, atype), aid in self.store.artifact_latest.items():
                if run_id != ctx.run_id or atype in ("intake", "review", "handoff_kit"):
                    continue
                record = self.store.artifacts.get(aid, {})
                for required_field in ("artifact_type", "format", "payload"):
                    if required_field not in record:
                        return False, f"artifact {aid} ({atype}) missing field '{required_field}'"
            return True, ""

        if gate == "completeness":
            # Verify every non-intake artifact payload is a non-empty dict
            for (run_id, atype), aid in self.store.artifact_latest.items():
                if run_id != ctx.run_id or atype in ("intake", "review", "handoff_kit"):
                    continue
                record = self.store.artifacts.get(aid, {})
                payload = record.get("payload")
                if not isinstance(payload, dict) or not payload:
                    return False, f"artifact {aid} ({atype}) has empty or invalid payload"
            return True, ""

        # Unknown gate names pass by default (forward compatible)
        return True, ""

    def create_revision_tasks_from_reviews(
        self, *, ctx: RunContext, review_ids: list[str], idempotency_key: str
    ) -> list[str]:
        del ctx
        return self._idempotent(idempotency_key, lambda: [f"revision_{rid}" for rid in review_ids])

    def persist_artifact(
        self, *, ctx: RunContext, artifact_payload: dict[str, Any], raw_bytes: bytes | None, idempotency_key: str
    ) -> ArtifactRef:
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
