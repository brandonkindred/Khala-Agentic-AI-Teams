from __future__ import annotations

from dataclasses import asdict

try:
    from temporalio import activity
except Exception:  # pragma: no cover
    class _ActivityShim:
        def defn(self, name=None):
            def decorator(fn):
                return fn

            return decorator

    activity = _ActivityShim()

from studiogrid.runtime.orchestrator import RunContext
from studiogrid.runtime.runtime_factory import build_orchestrator


def _key(scope: str = "default") -> str:
    return f"idempotency:{scope}"


def _ctx(ctx: dict, phase: str) -> RunContext:
    return RunContext(project_id=ctx["project_id"], run_id=ctx["run_id"], phase=phase, contract_version=ctx["contract_version"])


def _gates_for(phase: str) -> list[str]:
    return [f"{phase.lower()}_deterministic"]


@activity.defn(name="CreateProjectAndRun")
async def create_project_and_run(payload: dict) -> dict:
    orch = build_orchestrator()
    project_id = orch.create_project(name=payload["project_name"], idempotency_key=_key("create_project"))
    ctx = orch.create_run(project_id=project_id, idempotency_key=_key("create_run"))
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": payload["intake_payload"]},
        raw_bytes=None,
        idempotency_key=_key("persist_intake"),
    )
    return asdict(ctx)


@activity.defn(name="SetPhase")
async def set_phase(payload: dict) -> None:
    build_orchestrator().set_phase(run_id=payload["run_id"], phase=payload["phase"], idempotency_key=_key("set_phase"))


@activity.defn(name="RunPhase")
async def run_phase(payload: dict) -> None:
    orch = build_orchestrator()
    ctx = _ctx(payload["ctx"], payload["phase"])
    tasks = orch.build_phase_tasks(ctx=ctx)
    for task in tasks:
        orch.dispatch_task_to_agent(ctx=ctx, task=task, idempotency_key=_key(task["task_id"]))
    orch.run_gates_for_phase(ctx=ctx, gates=_gates_for(payload["phase"]), idempotency_key=_key("gates"))


@activity.defn(name="CreateApprovalDecision")
async def create_approval_decision(payload: dict) -> dict:
    decision_id = build_orchestrator().create_decision(
        run_id=payload["ctx"]["run_id"],
        title=f"Approve {payload['phase']} deliverables",
        context="Review the outputs and choose.",
        options=[
            {"key": "approve", "label": "Approve and continue"},
            {"key": "request_changes", "label": "Request changes"},
            {"key": "stop_project", "label": "Stop project"},
        ],
        idempotency_key=_key("decision"),
    )
    return {"decision_id": decision_id}


@activity.defn(name="GetDecision")
async def get_decision(payload: dict) -> dict:
    return build_orchestrator().get_decision(decision_id=payload["decision_id"])


@activity.defn(name="SetWaitingForHuman")
async def set_waiting(payload: dict) -> None:
    build_orchestrator().set_waiting_for_human(
        run_id=payload["run_id"],
        decision_id=payload["decision_id"],
        reason=payload["reason"],
        expires_at=None,
        idempotency_key=_key("waiting"),
    )


@activity.defn(name="SetRunning")
async def set_running(payload: dict) -> None:
    build_orchestrator().set_running(run_id=payload["run_id"], idempotency_key=_key("running"))


@activity.defn(name="AssembleHandoffKit")
async def assemble_handoff(payload: dict) -> dict:
    ref = build_orchestrator().assemble_handoff_kit(ctx=_ctx(payload["ctx"], "HANDOFF"), idempotency_key=_key("handoff"))
    return asdict(ref)
