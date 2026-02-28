from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from studiogrid.runtime.runtime_factory import build_orchestrator


def _print(data: dict) -> None:
    print(json.dumps(data))


def cmd_run_start(args: argparse.Namespace) -> None:
    orch = build_orchestrator()
    project_id = orch.create_project(name=args.project_name, idempotency_key=f"project:create:{args.project_name}")
    ctx = orch.create_run(project_id=project_id, idempotency_key=f"{project_id}:CreateRun:initial")
    intake = json.loads(Path(args.intake).read_text(encoding="utf-8"))
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": intake},
        raw_bytes=None,
        idempotency_key=f"{ctx.run_id}:PersistArtifact:intake:v1",
    )
    _print({"project_id": project_id, "run_id": ctx.run_id, "status": "RUNNING", "phase": "INTAKE"})


def cmd_run_status(args: argparse.Namespace) -> None:
    run = build_orchestrator().store.runs.get(args.run_id)
    if run is None:
        _print({"error": "run_not_found", "run_id": args.run_id})
        return
    _print(run)


def cmd_decision_list(args: argparse.Namespace) -> None:
    store = build_orchestrator().store
    decisions = [row for row in store.decisions.values() if row["run_id"] == args.run_id]
    _print({"run_id": args.run_id, "decisions": decisions})


def cmd_decision_choose(args: argparse.Namespace) -> None:
    orch = build_orchestrator()
    orch.resolve_decision(
        decision_id=args.decision_id,
        selected_option_key=args.option,
        idempotency_key=f"decision:resolve:{args.decision_id}:{args.option}",
    )
    _print(orch.get_decision(decision_id=args.decision_id))


async def cmd_workflow_signal_decision(args: argparse.Namespace) -> None:
    from temporalio.client import Client

    from studiogrid.runtime.temporal_workflow import StudioGridWorkflow

    client = await Client.connect(
        os.getenv("STUDIOGRID_TEMPORAL_SERVER", "localhost:7233"),
        namespace=os.getenv("STUDIOGRID_TEMPORAL_NAMESPACE", "default"),
    )
    handle = client.get_workflow_handle(args.run_id)
    await handle.signal(StudioGridWorkflow.decision_resolved, args.decision_id)
    _print({"run_id": args.run_id, "decision_id": args.decision_id, "signal": "decision_resolved"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studiogrid")
    sub = parser.add_subparsers(dest="group", required=True)

    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="action", required=True)
    start = run_sub.add_parser("start")
    start.add_argument("--project-name", required=True)
    start.add_argument("--intake", required=True)
    start.set_defaults(func=cmd_run_start)

    status = run_sub.add_parser("status")
    status.add_argument("--run-id", required=True)
    status.set_defaults(func=cmd_run_status)

    decision = sub.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="action", required=True)
    list_cmd = decision_sub.add_parser("list")
    list_cmd.add_argument("--run-id", required=True)
    list_cmd.set_defaults(func=cmd_decision_list)

    choose = decision_sub.add_parser("choose")
    choose.add_argument("--decision-id", required=True)
    choose.add_argument("--option", required=True)
    choose.set_defaults(func=cmd_decision_choose)

    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="action", required=True)
    signal = workflow_sub.add_parser("signal-decision")
    signal.add_argument("--run-id", required=True)
    signal.add_argument("--decision-id", required=True)
    signal.set_defaults(func=cmd_workflow_signal_decision)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    if asyncio.iscoroutine(result):
        asyncio.run(result)


if __name__ == "__main__":
    main()
