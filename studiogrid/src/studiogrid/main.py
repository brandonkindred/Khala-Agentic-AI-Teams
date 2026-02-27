from __future__ import annotations

import argparse
import asyncio
import json

from studiogrid.runtime.runtime_factory import build_orchestrator


def cmd_run_start(args: argparse.Namespace) -> None:
    orch = build_orchestrator()
    project_id = orch.create_project(name=args.project_name, idempotency_key=f"{args.project_name}:create")
    ctx = orch.create_run(project_id=project_id, idempotency_key=f"{project_id}:run")
    intake = json.loads(open(args.intake, "r", encoding="utf-8").read())
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": intake},
        raw_bytes=None,
        idempotency_key=f"{ctx.run_id}:intake",
    )
    print(json.dumps({"project_id": project_id, "run_id": ctx.run_id}))


def cmd_decision_choose(args: argparse.Namespace) -> None:
    orch = build_orchestrator()
    orch.resolve_decision(decision_id=args.decision_id, selected_option_key=args.option, idempotency_key=f"{args.decision_id}:resolve")
    print(json.dumps(orch.get_decision(decision_id=args.decision_id)))


def cmd_workflow_signal_decision(args: argparse.Namespace) -> None:
    from studiogrid.runtime.temporal_workflow import StudioGridWorkflow

    del StudioGridWorkflow
    print(json.dumps({"run_id": args.run_id, "decision_id": args.decision_id, "signal": "decision_resolved"}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studiogrid")
    sub = parser.add_subparsers(dest="group", required=True)

    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="action", required=True)
    start = run_sub.add_parser("start")
    start.add_argument("--project-name", required=True)
    start.add_argument("--intake", required=True)
    start.set_defaults(func=cmd_run_start)

    decision = sub.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="action", required=True)
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
