"""
Planning V3 orchestrator: phase order, state machine, adapter invocation.

Runs intake → discovery → requirements → synthesis → document_production → (optional) sub_agent_provisioning.
Uses shared LLM and adapters for PRA, Planning V2, Market Research, AI Systems.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from planning_v3_team.models import Phase

logger = logging.getLogger(__name__)

PHASE_ORDER = [
    Phase.INTAKE,
    Phase.DISCOVERY,
    Phase.REQUIREMENTS,
    Phase.SYNTHESIS,
    Phase.DOCUMENT_PRODUCTION,
    Phase.SUB_AGENT_PROVISIONING,
]


def run_workflow(
    repo_path: str,
    client_name: Optional[str] = None,
    initial_brief: Optional[str] = None,
    spec_content: Optional[str] = None,
    use_product_analysis: bool = True,
    use_planning_v2: bool = False,
    use_market_research: bool = False,
    capability_gap: Optional[str] = None,
    llm: Optional[Any] = None,
    job_updater: Optional[Callable[..., None]] = None,
    answer_callback: Optional[Callable[[list], list]] = None,
    run_architecture_fn: Optional[Callable[..., Optional[str]]] = None,
) -> Dict[str, Any]:
    """
    Run the full Planning V3 workflow.

    job_updater(current_phase, progress, status_text, ...) is called to report progress.
    answer_callback(pending_questions) is used when PRA is waiting for answers; return list of
    {question_id, selected_option_id?, other_text?}. If None, defaults are used (first option).
    Returns a result dict with success, handoff_package, summary, failure_reason, current_phase.
    """
    from planning_v3_team.adapters import (
        get_planning_v2_result,
        market_research_to_evidence,
        request_market_research,
        run_planning_v2,
        run_product_analysis,
        start_ai_systems_build,
        wait_for_ai_systems_build_completion,
        wait_for_planning_v2_completion,
        wait_for_product_analysis_completion,
    )
    from planning_v3_team.phases import (
        run_discovery,
        run_document_production,
        run_intake,
        run_requirements,
        run_sub_agent_provisioning,
        run_synthesis,
    )

    def _update(phase: str, progress: int, status_text: str = "") -> None:
        if job_updater:
            job_updater(current_phase=phase, progress=progress, status_text=status_text)

    context: Dict[str, Any] = {}
    result: Dict[str, Any] = {
        "success": False,
        "handoff_package": None,
        "summary": "",
        "failure_reason": "",
        "current_phase": None,
    }

    try:
        _update(Phase.INTAKE.value, 5, "Intake")
        ctx_update, _ = run_intake(
            repo_path=repo_path,
            client_name=client_name,
            initial_brief=initial_brief,
            spec_content=spec_content,
        )
        context.update(ctx_update)

        _update(Phase.DISCOVERY.value, 15, "Discovery")
        if llm:
            ctx_update, _ = run_discovery(context, llm)
            context.update(ctx_update)
        else:
            logger.warning("No LLM provided; skipping discovery refinement")

        _update(Phase.REQUIREMENTS.value, 25, "Requirements")
        if llm:
            ctx_update, _ = run_requirements(context, llm)
            context.update(ctx_update)

        _update(Phase.SYNTHESIS.value, 35, "Synthesis")
        market_evidence = None
        if use_market_research:
            client_ctx = context.get("client_context")
            problem = getattr(client_ctx, "problem_summary", None) if client_ctx else None
            users = getattr(client_ctx, "target_users", []) if client_ctx else []
            if problem or users:
                mr_data = request_market_research(
                    product_concept=problem or "Product",
                    target_users=", ".join(users) if users else "End users",
                    business_goal="Validate and refine requirements",
                )
                if mr_data:
                    market_evidence = market_research_to_evidence(mr_data)
        ctx_update, _ = run_synthesis(context, market_research_evidence=market_evidence)
        context.update(ctx_update)

        _update(Phase.DOCUMENT_PRODUCTION.value, 45, "Document production")
        def _pra_answer_cb(questions: list) -> list:
            if answer_callback:
                return answer_callback(questions)
            answers = []
            for q in questions:
                opts = q.get("options", [])
                if opts:
                    opt_id = next((o.get("id") for o in opts if o.get("is_default")), opts[0].get("id"))
                    answers.append({"question_id": q.get("id", ""), "selected_option_id": opt_id})
            return answers

        ctx_update, artifacts = run_document_production(
            context,
            use_product_analysis=use_product_analysis,
            use_planning_v2=use_planning_v2,
            run_pra=run_product_analysis,
            wait_pra=wait_for_product_analysis_completion,
            run_planning_v2_fn=run_planning_v2,
            wait_planning_v2_fn=wait_for_planning_v2_completion,
            get_planning_v2_result_fn=get_planning_v2_result,
            answer_callback=_pra_answer_cb,
            run_architecture_fn=run_architecture_fn,
        )
        context.update(ctx_update)
        result["handoff_package"] = context.get("handoff_package")
        if result["handoff_package"] and hasattr(result["handoff_package"], "model_dump"):
            result["handoff_package"] = result["handoff_package"].model_dump()

        _update(Phase.SUB_AGENT_PROVISIONING.value, 90, "Sub-agent provisioning (optional)")
        ctx_update, _ = run_sub_agent_provisioning(
            context,
            capability_gap=capability_gap,
            start_build_fn=start_ai_systems_build,
            wait_build_fn=wait_for_ai_systems_build_completion,
        )
        context.update(ctx_update)
        if context.get("sub_agent_blueprint") and result.get("handoff_package"):
            if isinstance(result["handoff_package"], dict):
                result["handoff_package"]["sub_agent_blueprint"] = context["sub_agent_blueprint"]

        result["success"] = True
        result["summary"] = "Planning V3 completed; handoff package ready."
        result["current_phase"] = Phase.DOCUMENT_PRODUCTION.value
        _update(Phase.DOCUMENT_PRODUCTION.value, 100, "Complete")
    except Exception as e:
        logger.exception("Planning V3 workflow failed")
        result["failure_reason"] = str(e)
        result["current_phase"] = context.get("current_phase")

    return result
