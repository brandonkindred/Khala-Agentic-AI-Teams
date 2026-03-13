"""
Document production phase: produce context doc and spec; call PRA and optionally Planning V2.

Persists artifacts to repo path and builds handoff package.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from planning_v3_team.models import ClientContext, HandoffPackage

logger = logging.getLogger(__name__)

CONTEXT_DOC_FILENAME = "client_context.md"
INITIAL_SPEC_FILENAME = "initial_spec.md"


def _write_context_document(repo_path: str, client_context: ClientContext) -> str:
    """Write client context as markdown; return path to file."""
    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    plan_dir = path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    out = plan_dir / CONTEXT_DOC_FILENAME
    lines = [
        "# Client & context",
        "",
        f"**Client:** {client_context.client_name or 'TBD'}",
        f"**Domain:** {client_context.client_domain or 'TBD'}",
        "",
        "## Problem & opportunity",
        (client_context.problem_summary or ""),
        "",
        (client_context.opportunity_statement or ""),
        "",
        "## Target users",
        *([f"- {u}" for u in (client_context.target_users or [])]),
        "",
        "## Success criteria",
        *([f"- {c}" for c in (client_context.success_criteria or [])]),
        "",
        "## Constraints",
        f"**RPO/RTO:** {client_context.rpo_rto or 'TBD'}",
        f"**SLAs:** {client_context.slas or 'TBD'}",
        f"**Compliance:** {client_context.compliance_notes or 'TBD'}",
        "",
        "## Assumptions",
        *([f"- {a}" for a in (client_context.assumptions or [])]),
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


def _write_initial_spec(repo_path: str, spec_content: str) -> str:
    """Write initial spec to repo; return path."""
    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    out = path / INITIAL_SPEC_FILENAME
    out.write_text(spec_content or "# Product specification\n\n(To be refined.)", encoding="utf-8")
    return str(out)


def run_document_production(
    context: Dict[str, Any],
    use_product_analysis: bool = True,
    use_planning_v2: bool = False,
    run_pra: Callable[..., Optional[str]] | None = None,
    wait_pra: Callable[..., Dict[str, Any]] | None = None,
    run_planning_v2_fn: Callable[..., Optional[str]] | None = None,
    wait_planning_v2_fn: Callable[..., Dict[str, Any]] | None = None,
    get_planning_v2_result_fn: Callable[..., Optional[Dict[str, Any]]] | None = None,
    answer_callback: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    run_architecture_fn: Optional[Callable[..., Optional[str]]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run document production: write context doc and spec; optionally call PRA and Planning V2.

    Adapters are injected (run_pra, wait_pra, etc.) so tests can mock them.
    answer_callback(pending_questions) should return list of {question_id, selected_option_id, other_text?}
    for PRA when waiting_for_answers. If None, PRA may block on questions.
    Returns (context_update, artifacts). artifacts includes handoff_package.
    """
    repo_path = context.get("repo_path", "")
    client_context = context.get("client_context")
    if isinstance(client_context, dict):
        client_context = ClientContext(**client_context)
    spec_content = context.get("spec_content") or ""
    initial_brief = context.get("initial_brief") or ""
    spec_to_use = spec_content or initial_brief or "# Specification\n\n(To be refined.)"

    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}
    client_context_doc_path: Optional[str] = None
    validated_spec_path: Optional[str] = None
    prd_path: Optional[str] = None
    planning_v2_artifact_paths: Dict[str, str] = {}

    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "plan").mkdir(parents=True, exist_ok=True)

    if client_context:
        client_context_doc_path = _write_context_document(repo_path, client_context)
        artifacts["client_context_document_path"] = client_context_doc_path

    initial_spec_path = _write_initial_spec(repo_path, spec_to_use)
    artifacts["initial_spec_path"] = initial_spec_path

    if use_product_analysis and run_pra and wait_pra:
        job_id = run_pra(repo_path=repo_path, spec_content=spec_to_use)
        if job_id:
            final = wait_pra(job_id=job_id, answer_callback=answer_callback)
            if final.get("status") == "completed":
                validated_spec_path = final.get("validated_spec_path")
                if not validated_spec_path:
                    validated_spec_path = str(Path(repo_path) / "plan" / "product_analysis" / "validated_spec.md")
                prd_path = str(Path(repo_path) / "plan" / "product_analysis" / "product_requirements_document.md")
            else:
                logger.warning("PRA did not complete: %s", final.get("error"))
        else:
            logger.warning("PRA run failed (no job_id)")
    else:
        validated_spec_path = initial_spec_path

    spec_for_planning_v2 = spec_to_use
    if validated_spec_path and Path(validated_spec_path).exists():
        spec_for_planning_v2 = Path(validated_spec_path).read_text(encoding="utf-8")

    if use_planning_v2 and run_planning_v2_fn and wait_planning_v2_fn and get_planning_v2_result_fn:
        p2_job_id = run_planning_v2_fn(spec_content=spec_for_planning_v2, repo_path=repo_path)
        if p2_job_id:
            p2_status = wait_planning_v2_fn(job_id=p2_job_id)
            if p2_status.get("status") == "completed":
                p2_result = get_planning_v2_result_fn(p2_job_id)
                if p2_result:
                    plan_dir = Path(repo_path) / "plan"
                    for name in ["architecture.md", "task_breakdown.md", "user_stories.md", "file_structure.md"]:
                        p = plan_dir / name
                        if p.exists():
                            planning_v2_artifact_paths[name] = str(p)
            else:
                logger.warning("Planning V2 did not complete: %s", p2_status.get("error"))

    def _read_if_exists(p: Optional[str]) -> Optional[str]:
        if not p:
            return None
        path = Path(p)
        return path.read_text(encoding="utf-8") if path.exists() else None

    architecture_overview: Optional[str] = None
    arch_path = planning_v2_artifact_paths.get("architecture.md")
    if arch_path:
        architecture_overview = _read_if_exists(arch_path)
        if architecture_overview and len(architecture_overview) > 8000:
            architecture_overview = architecture_overview[:8000] + "\n\n(truncated)"
    if architecture_overview is None and run_architecture_fn:
        try:
            spec_content_for_arch = _read_if_exists(validated_spec_path) or spec_to_use
            prd_content_for_arch = _read_if_exists(prd_path)
            cc_dict: Optional[Dict[str, Any]] = None
            if client_context and hasattr(client_context, "model_dump"):
                cc_dict = client_context.model_dump()
            elif isinstance(context.get("client_context"), dict):
                cc_dict = context["client_context"]
            architecture_overview = run_architecture_fn(
                spec_content=spec_content_for_arch or "",
                prd_content=prd_content_for_arch,
                repo_path=repo_path,
                client_context=cc_dict,
            )
            if architecture_overview and len(architecture_overview) > 8000:
                architecture_overview = architecture_overview[:8000] + "\n\n(truncated)"
        except Exception as e:
            logger.warning("Architecture step failed: %s", e)

    handoff = HandoffPackage(
        client_context=client_context,
        client_context_document_path=client_context_doc_path,
        validated_spec_path=validated_spec_path,
        validated_spec_content=_read_if_exists(validated_spec_path),
        prd_path=prd_path,
        prd_content=_read_if_exists(prd_path),
        planning_v2_artifact_paths=planning_v2_artifact_paths,
        architecture_overview=architecture_overview,
        summary="Handoff package produced by Planning V3.",
    )
    context_update["handoff_package"] = handoff
    artifacts["handoff_package"] = handoff.model_dump()
    return context_update, artifacts
