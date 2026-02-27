from __future__ import annotations

from dataclasses import asdict

from temporalio import activity

from studiogrid.runtime.orchestrator import RunContext
from studiogrid.runtime.runtime_factory import build_orchestrator


def _key(run_id: str, activity_name: str, scope: str) -> str:
    return f"{run_id}:{activity_name}:{scope}"


def _ctx(ctx: dict, phase: str) -> RunContext:
    return RunContext(
        project_id=ctx["project_id"],
        run_id=ctx["run_id"],
        phase=phase,
        contract_version=ctx["contract_version"],
    )


def _gates_for(phase: str) -> list[str]:
    return [f"{phase.lower()}_deterministic"]


@activity.defn(name="CreateProjectAndRun")
async def create_project_and_run(payload: dict) -> dict:
    orch = build_orchestrator()
    project_scope = payload["project_name"].replace(" ", "_").lower()
    project_id = orch.create_project(
        name=payload["project_name"],
        idempotency_key=f"project:CreateProjectAndRun:{project_scope}",
    )
    ctx = orch.create_run(project_id=project_id, idempotency_key=_key(project_id, "CreateRun", "initial"))
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": payload["intake_payload"]},
        raw_bytes=None,
        idempotency_key=_key(ctx.run_id, "PersistArtifact", "intake:v1"),
    )
    return asdict(ctx)


@activity.defn(name="SetPhase")
async def set_phase(payload: dict) -> None:
    run_id = payload["run_id"]
    phase = payload["phase"]
    build_orchestrator().set_phase(run_id=run_id, phase=phase, idempotency_key=_key(run_id, "SetPhase", phase))


@activity.defn(name="RunPhase")
async def run_phase(payload: dict) -> None:
    await _execute_phase(payload["ctx"], payload["phase"], payload.get("scope", payload["phase"]))


async def _execute_phase(ctx_payload: dict, phase: str, scope: str) -> None:
    orch = build_orchestrator()
    ctx = _ctx(ctx_payload, phase)
    tasks = orch.build_phase_tasks(ctx=ctx)
    for task in tasks:
        orch.dispatch_task_to_agent(
            ctx=ctx,
            task=task,
            idempotency_key=_key(ctx.run_id, "DispatchAgentTask", task["task_id"]),
        )
    gate_results = orch.run_gates_for_phase(
        ctx=ctx,
        gates=_gates_for(phase),
        idempotency_key=_key(ctx.run_id, "RunGates", scope),
    )
    if any(not result.passed for result in gate_results):
        review_ids = [rid for result in gate_results for rid in result.review_ids]
        orch.create_revision_tasks_from_reviews(
            ctx=ctx,
            review_ids=review_ids,
            idempotency_key=_key(ctx.run_id, "CreateRevisionTasks", scope),
        )


@activity.defn(name="CreateApprovalDecision")
async def create_approval_decision(payload: dict) -> dict:
    run_id = payload["ctx"]["run_id"]
    phase = payload["phase"]
    decision_id = build_orchestrator().create_decision(
        run_id=run_id,
        title=f"Approve {phase} deliverables",
        context="Review the outputs and choose.",
        options=[
            {"key": "approve", "label": "Approve and continue"},
            {"key": "request_changes", "label": "Request changes"},
            {"key": "stop_project", "label": "Stop project"},
        ],
        idempotency_key=_key(run_id, "CreateApprovalDecision", phase),
    )
    return {"decision_id": decision_id}


@activity.defn(name="GetDecision")
async def get_decision(payload: dict) -> dict:
    return build_orchestrator().get_decision(decision_id=payload["decision_id"])


@activity.defn(name="SetWaitingForHuman")
async def set_waiting(payload: dict) -> None:
    run_id = payload["run_id"]
    decision_id = payload["decision_id"]
    build_orchestrator().set_waiting_for_human(
        run_id=run_id,
        decision_id=decision_id,
        reason=payload["reason"],
        expires_at=None,
        idempotency_key=_key(run_id, "SetWaitingForHuman", decision_id),
    )


@activity.defn(name="SetRunning")
async def set_running(payload: dict) -> None:
    run_id = payload["run_id"]
    build_orchestrator().set_running(run_id=run_id, idempotency_key=_key(run_id, "SetRunning", "resume"))


@activity.defn(name="RunRevisionLoop")
async def run_revision_loop(payload: dict) -> None:
    revision_scope = f"{payload['phase']}:revision:{payload['decision_id']}"
    await _execute_phase(payload["ctx"], payload["phase"], revision_scope)


@activity.defn(name="FailRun")
async def fail_run(payload: dict) -> None:
    run_id = payload["run_id"]
    reason = payload["reason"]
    build_orchestrator().set_waiting_for_human(
        run_id=run_id,
        decision_id="stopped",
        reason=reason,
        expires_at=None,
        idempotency_key=_key(run_id, "FailRun", "terminal"),
    )


@activity.defn(name="AssembleHandoffKit")
async def assemble_handoff(payload: dict) -> dict:
    ctx = _ctx(payload["ctx"], "HANDOFF")
    ref = build_orchestrator().assemble_handoff_kit(
        ctx=ctx,
        idempotency_key=_key(ctx.run_id, "AssembleHandoffKit", "final"),
    )
    return asdict(ref)


@activity.defn(name="SetDone")
async def set_done(payload: dict) -> None:
    run_id = payload["run_id"]
    build_orchestrator().set_done(run_id=run_id, idempotency_key=_key(run_id, "SetDone", "terminal"))
