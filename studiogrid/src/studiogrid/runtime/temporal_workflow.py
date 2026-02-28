from __future__ import annotations

from datetime import timedelta

from temporalio import workflow


@workflow.defn
class StudioGridWorkflow:
    @workflow.run
    async def run(self, project_name: str, intake_payload: dict) -> dict:
        ctx = await workflow.execute_activity(
            "CreateProjectAndRun",
            {"project_name": project_name, "intake_payload": intake_payload},
            start_to_close_timeout=timedelta(minutes=5),
        )

        for phase in ["INTAKE", "DISCOVERY", "IA", "WIREFRAMES", "SYSTEM", "HIFI", "ASSETS", "HANDOFF"]:
            await workflow.execute_activity(
                "SetPhase",
                {"run_id": ctx["run_id"], "phase": phase},
                start_to_close_timeout=timedelta(minutes=2),
            )

            await workflow.execute_activity(
                "RunPhase",
                {"ctx": ctx, "phase": phase},
                start_to_close_timeout=timedelta(hours=2),
            )

            if phase in {"DISCOVERY", "HIFI", "HANDOFF"}:
                decision = await workflow.execute_activity(
                    "CreateApprovalDecision",
                    {"ctx": ctx, "phase": phase},
                    start_to_close_timeout=timedelta(minutes=5),
                )
                decision_id = decision["decision_id"]

                await workflow.execute_activity(
                    "SetWaitingForHuman",
                    {
                        "run_id": ctx["run_id"],
                        "decision_id": decision_id,
                        "reason": f"Approval for {phase}",
                    },
                    start_to_close_timeout=timedelta(minutes=2),
                )

                await workflow.wait_condition(lambda: self._resolved_decision_id == decision_id)

                chosen = await workflow.execute_activity(
                    "GetDecision",
                    {"decision_id": decision_id},
                    start_to_close_timeout=timedelta(minutes=2),
                )

                if chosen.get("selected_option_key") == "request_changes":
                    await workflow.execute_activity(
                        "RunRevisionLoop",
                        {"ctx": ctx, "phase": phase, "decision_id": decision_id},
                        start_to_close_timeout=timedelta(hours=2),
                    )

                if chosen.get("selected_option_key") == "stop_project":
                    await workflow.execute_activity(
                        "FailRun",
                        {"run_id": ctx["run_id"], "reason": "Stopped by human"},
                        start_to_close_timeout=timedelta(minutes=2),
                    )
                    return {"status": "STOPPED"}

                await workflow.execute_activity(
                    "SetRunning",
                    {"run_id": ctx["run_id"]},
                    start_to_close_timeout=timedelta(minutes=2),
                )

        handoff = await workflow.execute_activity(
            "AssembleHandoffKit",
            {"ctx": ctx},
            start_to_close_timeout=timedelta(minutes=15),
        )

        await workflow.execute_activity(
            "SetDone",
            {"run_id": ctx["run_id"]},
            start_to_close_timeout=timedelta(minutes=2),
        )
        return handoff

    @workflow.init
    def __init__(self) -> None:
        self._resolved_decision_id: str | None = None

    @workflow.signal
    async def decision_resolved(self, decision_id: str) -> None:
        self._resolved_decision_id = decision_id
