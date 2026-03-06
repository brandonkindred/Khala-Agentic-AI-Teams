from __future__ import annotations

import argparse
import json
from pathlib import Path

from studiogrid.runtime.registry_loader import RegistryLoader
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




def _build_registry() -> RegistryLoader:
    root = Path(__file__).resolve().parent
    return RegistryLoader(root)


def cmd_registry_list(args: argparse.Namespace) -> None:
    del args
    registry = _build_registry()
    _print({"agents": registry.list_agents()})


def cmd_registry_find(args: argparse.Namespace) -> None:
    registry = _build_registry()
    skills = [skill.strip() for skill in args.skills.split(",") if skill.strip()]
    candidates = registry.find_assisting_agents(
        problem_description=args.problem,
        required_skills=skills,
        limit=args.limit,
    )
    _print(
        {
            "problem": args.problem,
            "required_skills": skills,
            "assisting_agents": candidates,
            "should_spawn_sub_agents": len(candidates) == 0,
        }
    )

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

    registry = sub.add_parser("registry")
    registry_sub = registry.add_subparsers(dest="action", required=True)

    registry_list = registry_sub.add_parser("list")
    registry_list.set_defaults(func=cmd_registry_list)

    registry_find = registry_sub.add_parser("find")
    registry_find.add_argument("--problem", required=True)
    registry_find.add_argument("--skills", default="")
    registry_find.add_argument("--limit", type=int, default=5)
    registry_find.set_defaults(func=cmd_registry_find)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
